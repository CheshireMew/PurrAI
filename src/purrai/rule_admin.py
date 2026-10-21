"""Administration operations for rule admin."""

import re

from sqlalchemy import select

from purrai.errors import ManagementError
from purrai.models import (
    Rule,
    Whitelist,
    uid,
)
from purrai.rules import assess, domain, message_content
from purrai.schemas import (
    AdminInput,
    PreviewInput,
    RuleInput,
    WhitelistInput,
)
from purrai.serialization import row_dict


class RuleAdministration:
    def __init__(self, context):
        self.context = context

    async def rule(self, actor, group_id, body: RuleInput, identifier="", request_id=""):
        await self.context.access.authorize(actor, group_id, write=True)
        if body.kind in ("block_mention", "allow_mention"):
            if not re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", body.value):
                raise ManagementError(422, "引流账号请填写 @username 或 username，不要填写网址")
        elif body.kind != "keyword":
            try:
                if not domain(body.value) or any(c in body.value for c in (" ", "/", "?", "#")):
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise ManagementError(422, "域名规则请只填写主机名，例如 example.com") from exc
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id)
            if previous is not None:
                return previous
            row = await session.get(Rule, identifier) if identifier else Rule(id=uid(), group_id=group_id)
            if not row or row.group_id != group_id:
                raise ManagementError(404, "规则不存在")
            if not identifier:
                session.add(row)
            for key, value in body.model_dump().items():
                setattr(row, key, value)
            return await self.context.record(
                session, actor, group_id, "rule", "修改广告规则", row_dict(row), request_id
            )

    async def preview(self, actor, group_id, body: PreviewInput):
        group = await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            rules = (
                [
                    row_dict(row)
                    for row in (await session.scalars(select(Rule).where(Rule.group_id == group_id))).all()
                ]
                if body.rules is None
                else [r.model_dump() for r in body.rules]
            )
        text, urls = message_content({"text": body.text})
        result = assess(
            text,
            urls,
            rules,
            group.settings,
            body.newcomer,
            body.contact,
            body.repeats,
            body.similar,
            body.burst,
            body.sender_name,
        )
        return {
            "reasons": list(dict.fromkeys(result["reasons"] + result["name_reasons"])),
            "name_reasons": result["name_reasons"],
            "delete_message": bool(result["reasons"] or result["name_reasons"]),
            "mute_member": bool(result["name_reasons"] or result["mute_reasons"]) and not body.contact,
            "kick_member": body.contact,
        }

    async def whitelist(self, actor, group_id, body: WhitelistInput, request_id="", remove=False):
        await self.context.access.authorize(actor, group_id, write=True)
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id)
            if previous is not None:
                return previous
            row = await session.get(Whitelist, (group_id, body.user_id))
            if remove:
                if row:
                    await session.delete(row)
                result = {"ok": True, "user_id": body.user_id}
            else:
                if row is None:
                    row = Whitelist(group_id=group_id, **body.model_dump())
                    session.add(row)
                else:
                    for key, value in body.model_dump().items():
                        setattr(row, key, value)
                result = row_dict(row)
            return await self.context.record(
                session,
                actor,
                group_id,
                "whitelist",
                "移出白名单" if remove else "修改白名单",
                result,
                request_id,
            )

    async def admin(self, actor, body: AdminInput, request_id=""):
        await self.context.access.principal(actor, root=True)
        raise ManagementError(
            410, "群管理权限已改为自动识别，请直接在 Telegram 群中设置或撤销管理员；旧授权记录不再决定权限"
        )
