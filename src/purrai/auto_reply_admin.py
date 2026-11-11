"""Keyword reply management shared by Telegram and HTTP."""

from sqlalchemy import select

from purrai.auto_reply_schemas import AutoReplyInput
from purrai.auto_replies import matching_reply
from purrai.errors import ManagementError
from purrai.models import AutoReply, uid
from purrai.record_versions import check_version, versioned
from purrai.serialization import row_dict


class AutoReplyAdministration:
    def __init__(self, context):
        self.context = context

    async def page(self, actor, group_id, *, query="", page_number=1, size=20):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            rows = (await session.scalars(select(AutoReply).where(AutoReply.group_id == group_id)
                .order_by(AutoReply.priority.desc(), AutoReply.created_at, AutoReply.id))).all()
            needle = query.strip().casefold()
            items = [versioned(row_dict(row)) for row in rows
                     if not needle or needle in row.keyword.casefold() or needle in row.reply.casefold()]
            return {"items": items[(page_number - 1) * size:page_number * size],
                    "total": len(items), "page": page_number, "size": size}

    async def get(self, actor, group_id, identifier):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            return versioned(row_dict(await self.row(session, group_id, identifier)))

    @staticmethod
    async def row(session, group_id, identifier):
        row = await session.get(AutoReply, identifier)
        if row is None or row.group_id != group_id:
            raise ManagementError(404, "自动回复规则不存在")
        return row

    async def save(self, actor, group_id, body, identifier="", request_id="", expected_version=None, *, changes=None):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "auto_reply", "group_id": group_id, "id": identifier,
                  "body": changes if changes is not None else body.model_dump(), "version": expected_version}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            row = await self.row(session, group_id, identifier) if identifier else None
            if row is not None:
                check_version(row_dict(row), expected_version)
            if changes is not None:
                body = AutoReplyInput(**{**row_dict(row), **changes})
            if row is None:
                row = AutoReply(id=uid(), group_id=group_id)
                session.add(row)
            for key, value in body.model_dump().items():
                setattr(row, key, value)
            await session.flush()
            result = versioned(row_dict(row))
            return await self.context.record(session, actor, group_id, "auto_reply_rule",
                "保存关键词自动回复", result, request_id, intent)

    async def preview(self, actor, group_id, body):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            rules = [row_dict(row) for row in await session.scalars(select(AutoReply).where(AutoReply.group_id == group_id))]
        if body.draft is not None:
            rules = [body.draft.model_dump()]
        rule = matching_reply(body.text, rules)
        return {"matched": rule is not None, "rule": versioned(rule) if rule else None}
