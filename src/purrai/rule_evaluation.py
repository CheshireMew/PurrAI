"""Pure rule work runs outside the writer; its policy snapshot is checked on return."""

import asyncio
import copy
from dataclasses import dataclass

from purrai.advertising_patterns import source_titles
from purrai.models import Group, Violation
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
                contact=contact_should_block(message, settings, rules), source_titles=source_titles(message))
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
