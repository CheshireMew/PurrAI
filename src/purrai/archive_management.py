"""Archive quotas and explicit cleanup bound to a completed export receipt."""

import hashlib
import json

from sqlalchemy import delete, select

from purrai.archive import ARCHIVE_BYTES, archive_bytes, conditions, decorate, export_jsonl
from purrai.archive_capacity import group_bytes, limits
from purrai.errors import ManagementError
from purrai.models import ArchivedMessage, Group, MessageRevision, Meta, now, uid
from purrai.schemas import ArchiveFilter


def fingerprint(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class ArchiveManagement:
    def __init__(self, context):
        self.context = context

    async def capacity(self, actor, group_id):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            _, used = await archive_bytes(session)
            global_limit, group_limit = await limits(session, self.context.settings, group_id)
            _, own_used = await group_bytes(session, group_id)
            items = []
            if actor in self.context.settings.superadmin_ids:
                for group in await session.scalars(select(Group).order_by(Group.title)):
                    _, amount = await group_bytes(session, group.id)
                    _, allowance = await limits(session, self.context.settings, group.id)
                    items.append({"id": group.id, "title": group.title, "bytes": amount, "limit_bytes": allowance})
            return {"bytes": used, "limit_bytes": global_limit, "group_bytes": own_used,
                    "group_limit_bytes": group_limit, "groups": items,
                    "can_edit_quotas": actor in self.context.settings.superadmin_ids}

    async def quota(self, actor, group_id, body):
        await self.context.access.principal(actor, root=True)
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "archive_quota", "group_id": group_id, "body": body.model_dump(exclude={"request_id"})}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, body.request_id, intent)
            if previous is not None:
                return previous
            changes = {f"archive:quota:{group_id}": json.dumps(body.group_mb)}
            if body.global_mb is not None:
                changes["archive:global-limit-mb"] = str(body.global_mb)
            for key, value in changes.items():
                marker = await session.get(Meta, key)
                if marker:
                    marker.value = value
                else:
                    session.add(Meta(key=key, value=value))
            return await self.context.record(session, actor, group_id, "archive_quota", "修改归档额度",
                {"ok": True, "group_mb": body.group_mb, "global_mb": body.global_mb}, body.request_id, intent)

    async def stream(self, actor, group_id, filters, export_id=None):
        await self.context.access.authorize(actor, group_id)
        export_id = export_id or uid()
        cutoff = now()

        async def generate():
            digest, count = hashlib.sha256(), 0
            async for line in export_jsonl(self.context.db, group_id, filters, cutoff):
                digest.update(fingerprint(json.loads(line)))
                count += 1
                yield line
            # Only reaching the end of the response establishes a complete
            # export. A cancelled/partial download never authorizes cleanup.
            async with self.context.access.group_transaction(group_id) as session:
                data = {"actor_id": actor, "group_id": group_id, "cutoff": cutoff,
                        "filters": filters.model_dump(), "digest": digest.hexdigest(), "count": count,
                        "created_at": now(), "purged": False}
                session.add(Meta(key=f"archive-export:{export_id}", value=json.dumps(data), expires_at=now() + 86400))
        return export_id, generate()

    async def exports(self, actor, group_id):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            rows = (await session.scalars(select(Meta).where(Meta.key.startswith("archive-export:"),
                Meta.expires_at > now()).order_by(Meta.expires_at.desc()))).all()
        result = []
        for row in rows:
            data = json.loads(row.value)
            if data["actor_id"] == actor and data["group_id"] == group_id:
                result.append({"id": row.key.removeprefix("archive-export:"), **data})
        return result[:20]

    async def purge(self, actor, group_id, body):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "archive_purge", "group_id": group_id, "body": body.model_dump(exclude={"request_id"})}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, body.request_id, intent)
            if previous is not None:
                return previous
            receipt = await session.get(Meta, f"archive-export:{body.export_id}")
            data = json.loads(receipt.value) if receipt else None
            if (not data or receipt.expires_at <= now() or data["actor_id"] != actor
                    or data["group_id"] != group_id or data["purged"]):
                raise ManagementError(409, "请先完整导出本群消息，并选择自己最近 24 小时内的导出记录")
            if body.confirm_count != data["count"]:
                raise ManagementError(409, "确认条数与导出不一致，请重新核对")
            if data["count"] > 5000:
                raise ManagementError(422, "每次整理最多 5000 条，请按时间缩小导出范围后分批整理")
            rows = (await session.scalars(select(ArchivedMessage).where(
                *conditions(group_id, ArchiveFilter(**data["filters"])), ArchivedMessage.created_at <= data["cutoff"])
                .order_by(ArchivedMessage.sent_at, ArchivedMessage.message_id))).all()
            digest = hashlib.sha256()
            for start in range(0, len(rows), 100):
                for item in await decorate(session, rows[start:start + 100], True):
                    digest.update(fingerprint({"schema_version": 1, **item}))
            if len(rows) != data["count"] or digest.hexdigest() != data["digest"]:
                raise ManagementError(409, "导出后消息、版本或处置结果已变化，尚未清理；请重新导出再核对")
            for start in range(0, len(rows), 250):
                ids = [row.id for row in rows[start:start + 250]]
                await session.execute(delete(MessageRevision).where(MessageRevision.archive_id.in_(ids)))
                await session.execute(delete(ArchivedMessage).where(ArchivedMessage.id.in_(ids)))
                await session.execute(delete(Meta).where(Meta.key.in_([f"archive:gap:{identifier}" for identifier in ids])))
            await session.execute(delete(Meta).where(Meta.key.in_((ARCHIVE_BYTES, f"archive:group-bytes:{group_id}"))))
            await session.flush()
            _, total = await archive_bytes(session)
            _, own = await group_bytes(session, group_id)
            session.add_all([Meta(key=ARCHIVE_BYTES, value=str(total)), Meta(key=f"archive:group-bytes:{group_id}", value=str(own))])
            receipt.value = json.dumps({**data, "purged": True, "purged_at": now()})
            return await self.context.record(session, actor, group_id, "archive_purge", body.reason,
                {"ok": True, "removed": len(rows), "export_id": body.export_id}, body.request_id, intent)
