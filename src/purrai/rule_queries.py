"""Paged effective rules, evidence and atomic bulk changes."""

from sqlalchemy import func, select

from purrai.errors import ManagementError
from purrai.models import PromotionSource, Rule, RuleHit, uid
from purrai.record_versions import check_version
from purrai.rule_presentation import describe_rule
from purrai.schemas import RuleInput
from purrai.shared_policy import effective_rules, policy


class RuleQueries:
    def __init__(self, context):
        self.context = context

    async def page(self, actor, group_id, *, query="", kind="", enabled=None, source="", page_number=1, size=20):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            items = await effective_rules(session, group_id)
            matches = {row.rule_id: (row[1], row[2]) for row in await session.execute(select(
                RuleHit.rule_id, func.count(), func.max(RuleHit.created_at),
            ).where(RuleHit.group_id == group_id).group_by(RuleHit.rule_id))}
            learned = set(await session.scalars(select(PromotionSource.rule_id).distinct()))
            items = [describe_rule({**item, "hit_count": matches.get(item["id"], (0, 0))[0],
                     "last_hit_at": matches.get(item["id"], (0, 0))[1],
                     "origin": "learned" if item["id"] in learned else item.get("origin", "manual")}) for item in items]
            needle = query.strip().casefold()
            items = [item for item in items if (not needle or needle in item["value"].casefold()
                     or needle in item.get("context_value", "").casefold())
                     and (not kind or item["kind"] == kind) and (enabled is None or item["enabled"] == enabled)
                     and (not source or (source == "learned" and item["origin"] == "learned")
                          or (source == "shared" and item["inherited"])
                          or (source == "local" and not item["inherited"]))]
            items.sort(key=lambda item: item["id"])
            total = len(items)
            items = items[(page_number - 1) * size:page_number * size]
            ids = [item["id"] for item in items]
            sources = (await session.scalars(select(PromotionSource).where(PromotionSource.rule_id.in_(ids),
                *([] if actor in self.context.settings.superadmin_ids else [PromotionSource.group_id == group_id]))
                .order_by(PromotionSource.created_at.desc()).limit(200))).all() if ids else []
            for item in items:
                from purrai.group_history import historical_source
                item["sources"] = [{"group_id": row.group_id, "message_id": row.message_id,
                                    **(await historical_source(session, row.group_id, row.message_id)),
                                    "violation_id": row.violation_id, "created_at": row.created_at,
                                    "withdrawn": bool(row.withdrawn_at)} for row in sources if row.rule_id == item["id"]][:10]
            return {"items": items, "total": total, "page": page_number, "size": size}

    async def batch(self, actor, group_id, body):
        await self.context.access.authorize(actor, group_id, write=True)
        if body.scope == "global":
            await self.context.access.principal(actor, root=True)
        intent = {"operation": "rule_batch", "group_id": group_id, "body": body.model_dump(exclude={"request_id"})}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, body.request_id, intent)
            if previous is not None:
                return previous
            effective = {item["id"]: item for item in await effective_rules(session, group_id)}
            common = await policy(session)
            for identifier, expected in body.versions.items():
                item = effective.get(identifier)
                if item is None:
                    raise ManagementError(404, "规则不存在，请刷新列表")
                check_version(item, expected)
                if body.scope == "global":
                    if not item["inherited"]:
                        raise ManagementError(409, "批量修改通用规则只支持当前仍继承通用值的规则")
                else:
                    origin = identifier if item.get("inherited_from") else ""
                    row = await session.scalar(select(Rule).where(Rule.group_id == group_id,
                        Rule.inherited_from == origin)) if origin else await session.get(Rule, identifier)
                    if row is None:
                        row = Rule(id=uid(), group_id=group_id, inherited_from=origin)
                        session.add(row)
                    for key, value in RuleInput.model_validate(item).model_dump().items():
                        setattr(row, key, value)
                    row.enabled, row.customized = body.enabled, True
            if body.scope == "global":
                common.rules = [{**item, "enabled": body.enabled} if item["id"] in body.versions else item
                                for item in common.rules]
            return await self.context.record(session, actor, group_id, "rule", "批量启停规则",
                {"ok": True, "count": len(body.versions)}, body.request_id, intent)
