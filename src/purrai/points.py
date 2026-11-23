"""Transactional group points ledger, shared by HTTP and the offline CLI."""
import hashlib
import json

from sqlalchemy import func, select

from purrai.errors import ManagementError
from purrai.models import Group, PointsAccount, PointsEntry, PointsImport, PointsPacket, uid
from purrai.points_schemas import MAX_POINTS
from purrai.serialization import row_dict


class PointsLedger:
    def __init__(self, db):
        self.db = db

    @staticmethod
    async def group(session, group_id):
        if await session.get(Group, group_id) is None:
            raise ManagementError(404, "群不存在")

    @staticmethod
    async def account(session, group_id, account_id):
        row = await session.get(PointsAccount, account_id)
        if row is None or row.group_id != group_id:
            raise ManagementError(404, "积分账户不存在")
        return row

    @staticmethod
    async def previous(session, group_id, request_id, intent):
        row = await session.scalar(select(PointsEntry).where(
            PointsEntry.group_id == group_id, PointsEntry.request_id == request_id))
        if row and row.intent != intent:
            raise ManagementError(409, "请求编号已用于其他积分操作")
        return row.result if row else None

    @staticmethod
    async def entry(session, account, actor, request_id, kind, delta, reason, intent, *, previous_identity=None):
        await session.flush()
        result = row_dict(account)
        if previous_identity is not None:
            result["previous_identity"] = previous_identity
        session.add(PointsEntry(group_id=account.group_id, account_id=account.id, actor_id=actor,
            request_id=request_id, kind=kind, delta=delta, balance_after=account.balance,
            reason=reason, intent=intent, result=result))
        return result

    async def import_data(self, group_id, body, actor=0, *, preview=False):
        payload = body.model_dump()
        payload["rows"] = sorted(payload["rows"], key=lambda row: row["source_key"])
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        async with self.db.transaction() as session:
            await self.group(session, group_id)
            previous = await session.scalar(select(PointsImport).where(
                PointsImport.group_id == group_id, PointsImport.batch_key == body.batch_key))
            if previous:
                if previous.digest != digest:
                    raise ManagementError(409, "此批次已导入，但内容不同；请通过积分调整纠错")
                return {**row_dict(previous), "already_imported": True}
            keys = set(await session.scalars(select(PointsAccount.source_key).where(PointsAccount.group_id == group_id)))
            if keys.intersection(row.source_key for row in body.rows):
                raise ManagementError(409, "部分来源编号已入账，不能通过更换批次重复加分")
            users = set(await session.scalars(select(PointsAccount.user_id).where(
                PointsAccount.group_id == group_id, PointsAccount.user_id.is_not(None))))
            if users.intersection(row.user_id for row in body.rows if row.user_id is not None):
                raise ManagementError(409, "部分 Telegram ID 已有账户，请通过积分调整处理")
            result = {"batch_key": body.batch_key, "count": len(body.rows),
                      "total_points": sum(row.points for row in body.rows), "source": body.source,
                      "already_imported": False}
            if preview:
                return {**result, "preview": True}
            batch = PointsImport(group_id=group_id, batch_key=body.batch_key, digest=digest,
                                 source=body.source, count=result["count"], total_points=result["total_points"], actor_id=actor)
            session.add(batch)
            await session.flush()
            for item in body.rows:
                account = PointsAccount(group_id=group_id, source_key=item.source_key, display_name=item.display_name,
                    user_id=item.user_id, balance=item.points, source_rank=item.source_rank, note=item.note)
                session.add(account)
                await self.entry(session, account, actor, f"import:{batch.id}:{account.source_key}", "import",
                                 item.points, body.source, {"batch_id": batch.id, "source_key": item.source_key})
            return {**row_dict(batch), "already_imported": False}

    async def page(self, group_id, *, query="", pending=False, page_number=1, size=20):
        async with self.db.sessions() as session:
            await self.group(session, group_id)
            order = (PointsAccount.balance.desc(), PointsAccount.source_rank.asc().nulls_last(),
                     PointsAccount.created_at, PointsAccount.id)
            ranked = select(PointsAccount, func.row_number().over(order_by=order).label("rank")).where(
                PointsAccount.group_id == group_id).subquery()
            statement = select(ranked)
            if pending:
                statement = statement.where(ranked.c.user_id.is_(None))
            if query.strip():
                statement = statement.where(ranked.c.display_name.icontains(query.strip(), autoescape=True)
                    | (func.cast(ranked.c.user_id, PointsAccount.display_name.type) == query.strip())
                    | (ranked.c.source_key == query.strip()))
            total = await session.scalar(select(func.count()).select_from(statement.subquery()))
            rows = (await session.execute(statement.order_by(ranked.c.rank).offset((page_number - 1) * size).limit(size))).mappings().all()
            summary = (await session.execute(select(func.count(PointsAccount.id), func.coalesce(func.sum(PointsAccount.balance), 0),
                func.count(PointsAccount.user_id)).where(PointsAccount.group_id == group_id))).one()
            batches = (await session.scalars(select(PointsImport).where(PointsImport.group_id == group_id)
                .order_by(PointsImport.created_at.desc()).limit(20))).all()
            from dataclasses import asdict
            from purrai.points_trust import members_trust, PointsTrust
            trust = await members_trust(session, group_id, [row["user_id"] for row in rows])
            return {"items": [{**dict(row), "trust": asdict(trust.get(row["user_id"], PointsTrust()))} for row in rows],
                    "total": total, "page": page_number, "size": size,
                    "summary": {"accounts": summary[0], "points": summary[1], "pending": summary[0] - summary[2]},
                    "imports": [row_dict(batch) for batch in batches]}

    async def history(self, group_id, account_id, page_number=1):
        async with self.db.sessions() as session:
            account = await self.account(session, group_id, account_id)
            statement = select(PointsEntry).where(PointsEntry.account_id == account_id)
            total = await session.scalar(select(func.count()).select_from(statement.subquery()))
            rows = await session.scalars(statement.order_by(PointsEntry.created_at.desc(), PointsEntry.id.desc())
                                        .offset((page_number - 1) * 20).limit(20))
            return {"account": row_dict(account), "items": [row_dict(row) for row in rows], "total": total}

    async def create(self, group_id, body, actor, request_id):
        intent = {"kind": "create", "actor": actor, **body.model_dump()}
        async with self.db.transaction() as session:
            await self.group(session, group_id)
            previous = await self.previous(session, group_id, request_id, intent)
            if previous is not None:
                return previous
            if await session.scalar(select(PointsAccount.id).where(
                    PointsAccount.group_id == group_id, PointsAccount.user_id == body.user_id)):
                raise ManagementError(409, "该 Telegram ID 已有积分账户")
            account = PointsAccount(group_id=group_id, source_key=f"manual:{uid()}",
                user_id=body.user_id, display_name=body.display_name, balance=0)
            session.add(account)
            return await self.entry(session, account, actor, request_id, "create", 0, "创建积分账户", intent)

    async def change(self, group_id, account_id, body, actor, request_id, *, identity=False):
        kind = "identity" if identity else "adjustment"
        intent = {"kind": kind, "account_id": account_id, "actor": actor, **body.model_dump()}
        async with self.db.transaction() as session:
            previous = await self.previous(session, group_id, request_id, intent)
            if previous is not None:
                return previous
            account = await self.account(session, group_id, account_id)
            if account.version != body.version:
                raise ManagementError(409, "账户已更新，请刷新后再操作")
            before = None
            if identity:
                if account.user_id and account.user_id != body.user_id and await session.scalar(select(PointsPacket.id).where(
                    PointsPacket.group_id == group_id, PointsPacket.sender_id == account.user_id,
                    PointsPacket.refunded.is_(False), PointsPacket.remaining > 0)):
                    raise ManagementError(409, "此账户还有未结算红包，请结算后再更换绑定")
                if body.user_id is not None and await session.scalar(select(PointsAccount.id).where(
                    PointsAccount.group_id == group_id, PointsAccount.user_id == body.user_id, PointsAccount.id != account_id)):
                    raise ManagementError(409, "该 Telegram ID 已绑定另一个账户，请先核对身份")
                before = {"user_id": account.user_id, "display_name": account.display_name}
                account.user_id, account.display_name = body.user_id, body.display_name
            else:
                if not 0 <= account.balance + body.delta <= MAX_POINTS:
                    raise ManagementError(409, "积分不足或调整后超出允许范围")
                account.balance += body.delta
            account.version += 1
            return await self.entry(session, account, actor, request_id, kind, 0 if identity else body.delta,
                                    body.reason, intent, previous_identity=before)
