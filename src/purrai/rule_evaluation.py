"""Pure rule work runs outside the writer; its policy snapshot is checked on return."""

import asyncio
import copy
from dataclasses import dataclass

from sqlalchemy import select

from purrai.advertising_patterns import source_titles
from purrai.models import Action, ArchivedMessage, Group, MessageRevision, Violation
from purrai.message_buttons import has_inline_buttons
from purrai.advertising_constants import CONTACT_REASONS
from purrai.rules import assess, content_digest, message_content, similarity_signature
from purrai.shared_policy import effective_rules
from purrai.sender_identity import sender_name
from purrai.message_identity import message_sender
from purrai.solicitation_rules import contact_should_block


@dataclass
class MessageAssessment:
    settings: dict
    rules: list
    profile: dict
    ordinary: dict
    newcomer: dict
    digest: str
    similarity: tuple

    async def current(self, session, group):
        return self.settings == group.settings and self.rules == await effective_rules(session, group.id)


def evaluate_message(message, settings, rules):
    text, urls = message_content(message)
    sender = message_sender(message)
    args = dict(sender_name=sender_name(sender, include_username=True),
                contact=contact_should_block(message, settings, rules), source_titles=source_titles(message),
                inline_buttons=has_inline_buttons(message))
    profile = assess(text, urls, rules, settings, **args)
    args.update(forwarded=bool(message.get("forward_origin") or message.get("forward_date")
                              or message.get("forward_from") or message.get("forward_from_chat")
                              or message.get("forward_sender_name")),
                mentioned_users=[entity.get("user", {}) for entity in
                                 (message.get("entities") or message.get("caption_entities") or [])
                                 if entity.get("type") == "text_mention"])
    return MessageAssessment(settings, rules, profile, assess(text, urls, rules, settings, **args),
                             assess(text, urls, rules, settings, newcomer=True, **args),
                             content_digest(text) if text else "", similarity_signature(text))


async def prepare_message(db, message):
    async with db.sessions() as session:
        group = await session.get(Group, message["chat"]["id"])
        if not group or not group.approved or not group.enabled:
            return None
        settings = copy.deepcopy(group.settings)
        rules = copy.deepcopy(await effective_rules(session, group.id))
    return await asyncio.to_thread(evaluate_message, message, settings, rules)


async def rule_penalty_applies(session, action, group):
    evidence = action.payload.get("rule_evidence")
    if evidence is None:
        violation = await session.get(Violation, action.payload.get("violation_id"))
        if not violation:
            return False  # Unverifiable legacy work must not create a new permanent mute.
        evidence = {"text": violation.excerpt if violation.message_violation else "",
                    "sender_name": violation.sender_name, "urls": []}
    execution = session.info["action_execution"]
    for _ in range(3):
        settings = copy.deepcopy(group.settings)
        rules = copy.deepcopy(await effective_rules(session, group.id))
        result = await execution.external(lambda: asyncio.to_thread(
            assess, evidence.get("text", ""), evidence.get("urls", []), rules, settings,
            sender_name=evidence.get("sender_name", "")))
        if settings == group.settings and rules == await effective_rules(session, group.id):
            return bool(result["name_reasons"] if action.kind == "profile_mute" else result["mute_reasons"])
    raise ConnectionError("审核规则正在变化，稍后重新核实处罚")


async def message_penalty_applies(session, action, group):
    """Re-evaluate recorded message facts against current policy before effects."""
    original = (action.payload.get("message_evidence") or {}).get("message")
    if not original or not group or not group.enabled:
        return False

    async def latest():
        row = (await session.execute(select(MessageRevision.payload, MessageRevision.event_at,
                                           MessageRevision.is_edit, MessageRevision.update_id).join(
            ArchivedMessage, ArchivedMessage.id == MessageRevision.archive_id,
        ).where(ArchivedMessage.group_id == group.id, ArchivedMessage.message_id == original.get("message_id"))
          .order_by(MessageRevision.event_at.desc(), MessageRevision.is_edit.desc(),
                    MessageRevision.update_id.desc(), MessageRevision.id.desc()).limit(1))).first()
        evidence = action.payload["message_evidence"]
        current = evidence["message"]
        version = tuple(evidence.get("version", (current.get("edit_date", current["date"]), bool(current.get("edit_date")), 0)))
        # Archiving can be switched off between receipt and an edit. The stored
        # archive is then older than the pending action's refreshed evidence.
        return row[0] if row is not None and tuple(row[1:]) >= version else current

    execution = session.info["action_execution"]
    for _ in range(3):
        message = copy.deepcopy(await latest())
        if message.get("chat", {}).get("id") != group.id or message_sender(message).get("id") != action.user_id:
            return False
        settings, rules = copy.deepcopy(group.settings), copy.deepcopy(await effective_rules(session, group.id))
        result = await execution.external(lambda: asyncio.to_thread(evaluate_message, message, settings, rules))
        if not group.enabled or not group.approved:
            return False
        if (settings != group.settings or rules != await effective_rules(session, group.id)
                or message != await latest()):
            continue
        ordinary = result.ordinary
        if action.kind == "delete":
            return bool(ordinary["reasons"] or ordinary["name_reasons"])
        return bool(ordinary["ban_reasons"] or any(reason in CONTACT_REASONS for reason in ordinary["reasons"]))
    raise ConnectionError("消息或规则正在变化，稍后重新核实处罚")


async def refresh_message_evidence(session, message, update_id):
    """Keep pending message-based decisions current even when archive is off."""
    version = (message.get("edit_date", message["date"]), True, update_id)
    actions = await session.scalars(select(Action).where(
        Action.group_id == message["chat"]["id"], Action.state == "pending",
        Action.payload["message_evidence"]["message"]["message_id"].as_integer() == message["message_id"],
    ))
    for action in actions:
        evidence = action.payload["message_evidence"]
        old = evidence["message"]
        previous = tuple(evidence.get("version", (old.get("edit_date", old["date"]), bool(old.get("edit_date")), 0)))
        if version >= previous:
            action.payload = {**action.payload, "message_evidence": {
                **evidence, "message": copy.deepcopy(message), "version": list(version),
            }}
