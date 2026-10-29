"""Shared administration read models for HTTP and Telegram."""

from sqlalchemy import func, select

from purrai.action_results import action_result
from purrai.chat_budget import ChatBudget
from purrai.errors import ManagementError
from purrai.models import Action, Group, Inbox, Meta, Rule, Verification, Violation, Whitelist, now
from purrai.moderation_records import violation_actions
from purrai.serialization import row_dict
from purrai.record_operations import permanently_banned, verification_operations, violation_operations
from purrai.verification_state import ACTIVE
from purrai.shared_policy import effective_rule, effective_rules

RECORD_MODELS = {
    "pending": Verification,
    "verification": Verification,
    "rule": Rule,
    "violation": Violation,
    "whitelist": Whitelist,
    "action": Action,
}


class ManagementQueries:
    def __init__(self, context):
        self.context = context

    @staticmethod
    def model(value):
        return RECORD_MODELS[value] if isinstance(value, str) else value

    async def project(self, session, model, rows):
        result = []
        for row in rows:
            item = await action_result(session, row) if model is Action else row_dict(row)
            if model is Verification:
                for key in ("answer_hash", "input_digits", "nonce"):
                    item.pop(key, None)
                item["allowed_operations"] = verification_operations(item)
            if model is Violation:
                item["actions"] = [
                    row_dict(a) for a in await violation_actions(session, row.group_id, row.id)
                ]
                item["permanent_ban"] = permanently_banned(item["actions"])
                item["allowed_operations"] = violation_operations(item, item["permanent_ban"])
            result.append(item)
        return result

    async def page(
        self,
        actor,
        group_id,
        model,
        *,
        page_number=1,
        size=20,
        pending=False,
        state="",
        user_id=None,
        after=0,
        before=0,
        manual_only=False,
    ):
        await self.context.access.authorize(actor, group_id)
        model = self.model(model)
        if model is Rule:
            async with self.context.db.sessions() as session:
                items = sorted(await effective_rules(session, group_id), key=lambda item: item["id"])
            start = (page_number - 1) * size if size is not None else 0
            return {"items": items[start:start + size] if size is not None else items,
                    "total": len(items), "page": page_number, "size": size}
        filters = [model.group_id == group_id]
        if pending and model is Verification:
            filters.append(Verification.state.in_(ACTIVE))
        if state:
            filters.append(model.state == state)
        if user_id:
            filters.append(model.user_id == user_id)
        if after:
            filters.append(model.created_at >= after)
        if before:
            filters.append(model.created_at <= before)
        if manual_only:
            filters.append(Action.actor_id.is_not(None))
        order = (
            model.created_at.desc()
            if hasattr(model, "created_at")
            else (model.user_id if model is Whitelist else model.id)
        )
        async with self.context.db.sessions() as session:
            count = await session.scalar(select(func.count()).select_from(model).where(*filters))
            query = select(model).where(*filters).order_by(order)
            if size is not None:
                query = query.offset((page_number - 1) * size).limit(size)
            rows = (await session.scalars(query)).all()
            return {
                "items": await self.project(session, model, rows),
                "total": count,
                "page": page_number,
                "size": size,
            }

    async def records(self, actor, group_id, model, page=0, pending=False):
        result = await self.page(actor, group_id, model, page_number=page + 1, size=5, pending=pending)
        return result["items"], result["total"]

    async def load_record(self, session, group_id, model, identifier):
        if model is Rule:
            item = await effective_rule(session, group_id, identifier)
            if item is None:
                raise ManagementError(404, "规则不存在，请刷新列表")
            return Rule(**{key: value for key, value in item.items() if key in Rule.__table__.columns})
        key = (group_id, int(identifier)) if model is Whitelist else identifier
        row = await session.get(model, key)
        if row is None or row.group_id != group_id:
            raise ManagementError(404, "记录不存在，请刷新列表")
        return row

    async def single(self, actor, group_id, model, identifier):
        await self.context.access.authorize(actor, group_id)
        model = self.model(model)
        async with self.context.db.sessions() as session:
            return await self.load_record(session, group_id, model, identifier)

    async def detail(self, actor, group_id, model, identifier):
        await self.context.access.authorize(actor, group_id)
        model = self.model(model)
        async with self.context.db.sessions() as session:
            row = await self.load_record(session, group_id, model, identifier)
            return (await self.project(session, model, [row]))[0]

    async def action(self, actor, identifier):
        async with self.context.db.sessions() as session:
            row = await session.get(Action, identifier)
            if row is None:
                raise ManagementError(404, "操作不存在")
            if row.group_id is None:
                await self.context.access.principal(actor, root=True)
            else:
                await self.context.access.authorize(actor, row.group_id)
            return await action_result(session, row)

    async def pending_group(self, actor, group_id):
        await self.context.access.principal(actor, root=True)
        async with self.context.db.sessions() as session:
            row = await session.get(Group, group_id)
        if row is None:
            raise ManagementError(404, "群不存在")
        return row

    async def action_statuses(self, actor, identifiers):
        items, errors = [], {}
        for identifier in dict.fromkeys(identifiers):
            try:
                items.append(await self.action(actor, identifier))
            except ManagementError as exc:
                errors[identifier] = {"status": exc.status, "detail": str(exc)}
        return {"items": items, "errors": errors}

    async def failures(self, actor, *, event=False, page=0, size=6):
        await self.context.access.principal(actor, root=True)
        model = Inbox if event else Action
        async with self.context.db.sessions() as session:
            return (
                await session.scalars(
                    select(model)
                    .where(model.state == "failed")
                    .order_by(model.id)
                    .offset(page * 5)
                    .limit(size)
                )
            ).all()

    async def system(self, actor):
        await self.context.access.principal(actor, root=True)
        async with self.context.db.sessions() as session:
            counts = dict(
                (await session.execute(select(Action.state, func.count()).group_by(Action.state))).all()
            )
            failed = (
                await session.scalars(
                    select(Action)
                    .where(Action.state == "failed")
                    .order_by(Action.updated_at.desc())
                    .limit(30)
                )
            ).all()
            events = (await session.scalars(select(Inbox).where(Inbox.state == "failed").limit(30))).all()
            backup, archive = (
                await session.get(Meta, "last_backup"),
                await session.get(Meta, "archive:paused"),
            )
            result = {
                "pending_actions": counts.get("pending", 0),
                "failed_count": counts.get("failed", 0),
                "failed_actions": [row_dict(row) for row in failed],
                "failed_events": [
                    {"id": row.id, "error": row.error, "attempts": row.attempts} for row in events
                ],
                "last_backup": backup.value if backup else None,
                "archive_status": archive.value if archive else "",
            }
        result["chat_budget"] = await ChatBudget(self.context.db, self.context.settings).status(None)
        return result

    async def dashboard(self, actor):
        identifiers = (
            None
            if actor in self.context.settings.superadmin_ids
            else [row["id"] for row in await self.context.access.groups(actor)]
        )
        if identifiers == []:
            raise ManagementError(403, "你目前不是机器人已加入群的管理员，请先在 Telegram 群中设置管理员身份")
        async with self.context.db.sessions() as session:

            async def count(model, *conditions):
                query = select(func.count()).select_from(model).where(*conditions)
                if identifiers is not None:
                    query = query.where((model.id if model is Group else model.group_id).in_(identifiers))
                return await session.scalar(query)

            return {
                "groups": await count(Group),
                "active_groups": await count(Group, Group.enabled.is_(True)),
                "pending": await count(Verification, Verification.state.in_(ACTIVE)),
                "passed_today": await count(
                    Verification, Verification.state == "passed", Verification.updated_at >= now() - 86400
                ),
                "failed_today": await count(
                    Verification, Verification.state == "failed", Verification.updated_at >= now() - 86400
                ),
                "violations_today": await count(Violation, Violation.created_at >= now() - 86400),
                "failed_actions": await count(Action, Action.state == "failed"),
            }
