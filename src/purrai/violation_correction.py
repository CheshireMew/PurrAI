"""Explicit, atomic correction of a violation and its learned destinations."""

import json

from sqlalchemy import func, select

from purrai.action_queue import enqueue
from purrai.deletion_effects import deletion_result
from purrai.errors import ManagementError
from purrai.models import Action, Group, Member, Meta, PromotionSource, Rule, Violation, now
from purrai.record_versions import check_version, record_version
from purrai.restriction_state import restriction_identity
from purrai.rule_presentation import describe_rule
from purrai.shared_policy import effective_rules, policy


class ViolationCorrection:
    def __init__(self, context):
        self.context = context

    @staticmethod
    async def linked_rules(session, group_id, violation):
        identifiers = set(await session.scalars(select(PromotionSource.rule_id).where(
            PromotionSource.group_id == group_id, PromotionSource.message_id == violation.message_id)))
        common = (await policy(session)).rules
        effective = {item["id"]: item for item in await effective_rules(session, group_id)}
        return [{**describe_rule(effective[item["id"]]), "global_version": record_version(item)}
                for item in common if item["id"] in identifiers and item["id"] in effective]

    async def preview(self, actor, group_id, identifier):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            violation = await session.get(Violation, identifier)
            if not violation or violation.group_id != group_id:
                raise ManagementError(404, "违规记录不存在")
            rules = await self.linked_rules(session, group_id, violation)
            for rule in rules:
                rule["other_sources"] = await session.scalar(select(func.count()).select_from(PromotionSource).where(
                    PromotionSource.rule_id == rule["id"], PromotionSource.withdrawn_at == 0,
                    (PromotionSource.group_id != group_id) | (PromotionSource.message_id != violation.message_id)))
            groups = (await session.scalars(select(Group))).all()
            overrides = {(row.group_id, row.inherited_from) for row in await session.scalars(select(Rule).where(
                Rule.customized.is_(True), Rule.inherited_from != ""))}
            return {"rules": rules, "global_allowed": actor in self.context.settings.superadmin_ids,
                    "affected_groups": [{"id": group.id, "title": group.title,
                                         "rule_ids": [rule["id"] for rule in rules if (group.id, rule["id"]) not in overrides]}
                                        for group in groups
                                        if group.approved] if actor in self.context.settings.superadmin_ids else [],
                    "user_id": violation.user_id, "excerpt": violation.excerpt}

    async def apply(self, actor, group_id, identifier, body):
        await self.context.access.authorize(actor, group_id, write=True)
        if body.scope == "global":
            await self.context.access.principal(actor, root=True)
        intent = {"operation": "violation_correction", "violation_id": identifier, "group_id": group_id,
                  "body": body.model_dump(exclude={"request_id"})}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, body.request_id, intent)
            if previous is not None:
                return previous
            violation = await session.get(Violation, identifier)
            if not violation or violation.group_id != group_id:
                raise ManagementError(404, "违规记录不存在")
            linked = {rule["id"]: rule for rule in await self.linked_rules(session, group_id, violation)}
            if body.rule_versions.keys() - linked.keys():
                raise ManagementError(422, "只能处理由这条消息关联的推广目标")
            common = await policy(session)
            updated = []
            for rule_id, expected in body.rule_versions.items():
                rule = linked[rule_id]
                if body.scope == "global":
                    original = next(item for item in common.rules if item["id"] == rule_id)
                    check_version(original, expected)
                    updated.append(rule_id)
                else:
                    check_version(rule, expected)
                    row = await session.scalar(select(Rule).where(
                        Rule.group_id == group_id, Rule.inherited_from == rule_id))
                    if row is None:
                        from purrai.models import uid
                        row = Rule(id=uid(), group_id=group_id, inherited_from=rule_id)
                        session.add(row)
                    from purrai.schemas import RuleInput
                    for key, value in RuleInput.model_validate(rule).model_dump().items():
                        setattr(row, key, value)
                    row.enabled, row.customized = False, True
            if updated:
                common.rules = [{**rule, "enabled": False} if rule["id"] in updated else rule for rule in common.rules]
            for source in await session.scalars(select(PromotionSource).where(
                PromotionSource.group_id == group_id, PromotionSource.message_id == violation.message_id)):
                source.withdrawn_at = now()
            for action in await session.scalars(select(Action).where(
                Action.group_id == group_id, Action.actor_id.is_(None), Action.state == "pending",
                Action.payload["violation_id"].as_string() == identifier)):
                action.state, action.error = "cancelled", "管理员已标记误判：" + body.reason
                await deletion_result(session, action)
            action_ids = []
            if body.action:
                member = await session.get(Member, (group_id, violation.user_id))
                owned = member.owned_restriction if member else None
                source = await session.get(Action, owned["source_action"]) if owned and owned.get("source_action") else None
                if not source or source.payload.get("violation_id") != identifier:
                    raise ManagementError(409, "原限制已结束或被替换，请核实成员当前状态；规则修改尚未保存")
                action = await enqueue(session, body.action, f"correction:{actor}:{body.request_id}",
                    group_id, violation.user_id, {"violation_id": identifier, "generation": member.generation,
                    "restriction": restriction_identity(member)}, 0, actor, body.reason)
                action_ids.append(action.id)
            marker = await session.get(Meta, f"violation-correction:{identifier}")
            correction = {"actor_id": actor, "reason": body.reason, "created_at": now(),
                          "scope": body.scope, "disabled_rules": list(body.rule_versions), "action_ids": action_ids}
            if marker:
                marker.value = json.dumps(correction, ensure_ascii=False)
            else:
                session.add(Meta(key=f"violation-correction:{identifier}", value=json.dumps(correction, ensure_ascii=False)))
            await session.flush()
            return await self.context.record(session, actor, group_id, "violation_correction", body.reason,
                                             correction, body.request_id, intent)
