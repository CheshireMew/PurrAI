"""Editable global business policy with preview, versions and audited writes."""

import asyncio
import copy
import hashlib
import json

from purrai.business_rules import RuleSet
from purrai.chat_input import input_reasons
from purrai.errors import ManagementError
from purrai.models import SharedPolicy
from purrai.rule_evaluation import evaluate_message
from purrai.shared_policy import effective_rules, policy
from purrai.rules import CONTACT_REASONS


def policy_result(document):
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"document": copy.deepcopy(document), "version": hashlib.sha256(encoded.encode()).hexdigest()}


def evaluate_preview(message, settings, rules, direct):
    result = evaluate_message(message, settings, rules).ordinary
    ban = bool(result["ban_reasons"] or any(reason in CONTACT_REASONS for reason in result["reasons"]))
    return {**result, "delete_message": bool(result["reasons"] or result["name_reasons"]),
            "ban_member": ban, "mute_member": not ban and bool(result["name_reasons"] or result["mute_reasons"]),
            "chat_reasons": input_reasons(message["text"], rules, settings, direct=direct)}


class BusinessAdministration:
    def __init__(self, context):
        self.context = context

    async def read(self, actor):
        await self.context.access.principal(actor, root=True)
        async with self.context.db.sessions() as session:
            return policy_result((await policy(session)).business_rules)

    async def save(self, actor, body, expected_version):
        await self.context.access.principal(actor, root=True)
        async with self.context.db.transaction() as session:
            intent = {"operation": "business_policy", "document": body.document, "version": expected_version}
            previous = await self.context.previous(session, actor, body.request_id, intent)
            if previous is not None:
                return previous
            current = await policy(session)
            if expected_version != policy_result(current.business_rules)["version"]:
                raise ManagementError(409, "业务规则已被修改，请刷新核对；你的草稿已保留")
            if current not in session:
                current = SharedPolicy(id=1, settings=current.settings, rules=current.rules)
                session.add(current)
            current.business_rules = copy.deepcopy(body.document)
            return await self.context.record(session, actor, None, "rule", "修改业务规则",
                policy_result(current.business_rules), body.request_id, intent)

    async def preview(self, actor, body):
        await self.context.access.principal(actor, root=True)
        group = await self.context.access.authorize(actor, body.group_id)
        async with self.context.db.sessions() as session:
            rules = copy.deepcopy(await effective_rules(session, group.id))
        message = {"text": body.text, "from": {"first_name": body.sender_name},
                   "chat": {"id": group.id, "type": "supergroup"}}
        if body.contact:
            message["contact"] = {"first_name": body.contact_name}
        if body.source_title:
            message["forward_origin"] = {"chat": {"type": "channel", "title": body.source_title}}
        before = await asyncio.to_thread(evaluate_preview, message, group.settings, rules, body.direct)
        draft = RuleSet(rules, copy.deepcopy(body.document))
        after = await asyncio.to_thread(evaluate_preview, message, group.settings, draft, body.direct)
        return {"before": before, "after": after, "changed": before != after}
