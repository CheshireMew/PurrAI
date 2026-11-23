"""Authorized, idempotent administration shared by Telegram and HTTP."""
from sqlalchemy import func, select

from purrai.admin_context import AdminContext
from purrai.errors import ManagementError
from purrai.lottery import (cancel, close, document, draw, entry_document, notice, points_available,
                            remaining_prizes, require_row)
from purrai.models import Action, Lottery, LotteryEntry, now


class LotteryAdmin:
    def __init__(self, management):
        self.management, self.db = management, management.db
        self.context = AdminContext(management.db, management.tg, management.settings, management.access)

    async def page(self, actor, group_id, page=1, size=20):
        await self.management.authorize(actor, group_id)
        async with self.db.sessions() as session:
            query = select(Lottery).where(Lottery.group_id == group_id)
            total = await session.scalar(select(func.count()).select_from(query.subquery()))
            rows = await session.scalars(query.order_by(Lottery.created_at.desc(), Lottery.id)
                .offset((page - 1) * size).limit(size))
            return {"items": [document(row) for row in rows], "total": total}

    async def get(self, actor, group_id, identifier, page=1, entry_state="", size=50):
        await self.management.authorize(actor, group_id)
        async with self.db.sessions() as session:
            row = await require_row(session, group_id, identifier)
            query = select(LotteryEntry).where(LotteryEntry.lottery_id == row.id)
            if entry_state:
                query = query.where(LotteryEntry.state == entry_state)
            total = await session.scalar(select(func.count()).select_from(query.subquery()))
            entries = await session.scalars(query.order_by(LotteryEntry.created_at.desc(), LotteryEntry.id)
                .offset((page - 1) * size).limit(size))
            deliveries = await session.scalars(select(Action).where(Action.kind == "lottery_notice",
                Action.payload["lottery_id"].as_string() == row.id).order_by(Action.created_at.desc()).limit(5))
            return {**document(row), "remaining": await remaining_prizes(session, row),
                "entries": [entry_document(item) for item in entries], "total_entries": total,
                "deliveries": [{"id": item.id, "state": item.state, "error": item.error,
                    "phase": item.payload["phase"]} for item in deliveries]}

    async def save(self, actor, group_id, body, request_id, identifier=None):
        await self.management.authorize(actor, group_id, write=True)
        intent = {"operation": "lottery_save", "group_id": group_id, "id": identifier, **body.model_dump()}
        async with self.db.transaction() as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            if identifier:
                row = await require_row(session, group_id, identifier)
                if row.state != "draft" or row.version != body.version:
                    raise ManagementError(409, "只能修改当前版本的草稿；已发布活动的规则不能修改")
                row.config, row.version = body.config.model_dump(), row.version + 1
            else:
                if body.version:
                    raise ManagementError(409, "新建抽奖的版本须为 0")
                row = Lottery(group_id=group_id, config=body.config.model_dump(), created_by=actor)
                session.add(row)
            await session.flush()
            return await self.context.record(session, actor, group_id, "lottery_manage", "保存抽奖草稿",
                document(row), request_id, intent)

    async def operate(self, actor, group_id, identifier, body, request_id):
        await self.management.authorize(actor, group_id, write=True)
        intent = {"operation": "lottery_operate", "group_id": group_id, "id": identifier, **body.model_dump()}
        async with self.db.transaction() as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            row = await require_row(session, group_id, identifier)
            if row.version != body.version:
                raise ManagementError(409, "抽奖状态已变化，请刷新后再操作")
            operation = body.operation
            if operation == "copy":
                config = {**row.config, "prizes": [dict(prize) for prize in row.config["prizes"]]}
                # Keep the schedule type but require a fresh timestamp before publication.
                if config["draw_mode"] == "time":
                    config["ends_at"] = 0
                row = Lottery(group_id=group_id, config=config, created_by=actor)
                session.add(row)
            elif operation == "publish":
                from purrai.lottery_schemas import LotteryConfig
                config = LotteryConfig.model_validate(row.config)
                if row.state != "draft":
                    raise ManagementError(409, "只有草稿可以发布")
                if config.draw_mode == "time" and config.ends_at <= now():
                    raise ManagementError(422, "结束时间必须晚于当前时间")
                await points_available(session, group_id, config)
                row.state, row.opened_at, row.version = "open", now(), row.version + 1
                await notice(session, row, "announcement", request_id)
            elif operation == "draw":
                await draw(session, row)
            elif operation == "cancel":
                await cancel(session, row)
            elif operation == "close":
                await close(session, row)
            elif operation == "announce":
                if row.state == "draft":
                    raise ManagementError(409, "请先发布活动")
                await notice(session, row, "announcement" if row.state == "open" else "result", request_id)
                row.version += 1
            elif operation == "fulfill":
                entry = await session.get(LotteryEntry, body.entry_id)
                if (not entry or entry.lottery_id != row.id or entry.state != "won" or entry.reward_points):
                    raise ManagementError(409, "只能登记人工发放的中奖奖品")
                entry.fulfilled = True
                row.version += 1
            await session.flush()
            return await self.context.record(session, actor, group_id, "lottery_manage", "抽奖管理",
                document(row), request_id, intent)
