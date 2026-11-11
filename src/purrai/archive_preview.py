"""Run saved message metadata through current rules and one unsaved rule overlay."""

import asyncio
import copy
from types import SimpleNamespace

from sqlalchemy import select

from purrai.advertising_guard import link_reasons, message_links
from purrai.advertising_patterns import is_ban_reason
from purrai.archive import detail
from purrai.errors import ManagementError
from purrai.group_link_identity import own_channel_id, own_link_keys
from purrai.models import Action
from purrai.rule_evaluation import evaluate_message
from purrai.shared_policy import effective_rules
from purrai.telegram_sources import channel_sources, external_channel_reasons
from purrai.rules import CONTACT_REASONS
from purrai.business_rules import with_rules


def evaluate_saved(message, settings, rules, newcomer, own_keys, linked, proofs):
    result = evaluate_message(message, settings, rules)
    assessment = result.newcomer if newcomer else result.ordinary
    reasons = list(dict.fromkeys(assessment["reasons"] + assessment["name_reasons"]))
    if settings.get("external_group_ban", True):
        reasons += external_channel_reasons(channel_sources(message), message["chat"]["id"], settings, rules, linked_channel_id=linked)
    reasons += link_reasons(message, SimpleNamespace(id=message["chat"]["id"], settings=settings), rules, own_keys, proofs)
    ban = any(reason in CONTACT_REASONS or is_ban_reason(reason) for reason in reasons)
    sender_chat = bool(message.get("sender_chat"))
    return {"reasons": list(dict.fromkeys(reasons)), "delete_message": bool(reasons),
            "ban_member": ban and not sender_chat,
            "mute_member": not sender_chat and not ban and bool(assessment["name_reasons"] or assessment["mute_reasons"])}


class ArchivePreview:
    def __init__(self, context):
        self.context = context

    async def run(self, actor, group_id, body):
        group = await self.context.access.authorize(actor, group_id)
        from purrai.rule_admin import RuleAdministration
        if body.draft:
            RuleAdministration.validate(body.draft)
        async with self.context.db.sessions() as session:
            rules = copy.deepcopy(await effective_rules(session, group_id))
            keys = await own_link_keys(session, group_id)
            linked = await own_channel_id(session, group_id)
        draft_rules = with_rules(rules, [rule for rule in rules if not body.draft or rule.get("id") != body.draft.id])
        if body.draft:
            draft_rules.append(body.draft.model_dump())
        items = []
        for identifier in dict.fromkeys(body.archive_ids):
            item = await detail(self.context.db, group_id, identifier)
            if item is None:
                raise ManagementError(404, "所选归档消息不属于本群或已被整理")
            revision = max(item["revisions"], key=lambda row: (row["event_at"], row["is_edit"], row["update_id"], row["id"]), default=None)
            if revision is None:
                raise ManagementError(409, "该归档没有可测试的版本")
            message = copy.deepcopy(revision["payload"])
            message.setdefault("chat", {"id": group_id, "type": "supergroup"})
            message["chat"]["id"] = group_id
            async with self.context.db.sessions() as session:
                action = await session.scalar(select(Action).where(Action.dedupe_key == f"advertising-check:{group_id}:{item['message_id']}"))
                proofs = action.payload.get("evidence", []) if action else []
            args = (message, copy.deepcopy(group.settings))
            before = await asyncio.to_thread(evaluate_saved, *args, rules, body.newcomer, keys, linked, proofs)
            after = await asyncio.to_thread(evaluate_saved, *args, draft_rules, body.newcomer, keys, linked, proofs)
            items.append({"id": identifier, "message_id": item["message_id"], "text": item["text"] or item["caption"],
                          "before": before, "after": after, "changed": before != after,
                          "incomplete": bool(item.get("recording_gap") or not item["original_observed"]),
                          "unresolved_links": bool(message_links(message) and not proofs)})
        return {"items": items, "changed_count": sum(item["changed"] for item in items),
                "notes": ["按当前规则与本次新成员假设试跑，不重建历史刷屏次数或发图资格。",
                          "只使用已保存的链接核实证据，不发起 Telegram 查询、模型请求、处罚或黑名单学习。实际执行仍核实管理员及白名单。"]}
