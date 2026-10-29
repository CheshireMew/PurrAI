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
from purrai.telegram_links import extract_links
from purrai.schemas import (
    AdminInput,
    PreviewInput,
    RuleInput,
    WhitelistInput,
)
from purrai.serialization import row_dict
from purrai.shared_policy import effective_rule, effective_rules


class RuleAdministration:
    def __init__(self, context):
        self.context = context

    async def inherit(self, actor, group_id, identifier):
        await self.context.access.authorize(actor, group_id, write=True)
        async with self.context.access.group_transaction(group_id) as session:
            item = await effective_rule(session, group_id, identifier)
            if item is None:
                raise ManagementError(404, "规则不存在")
            from purrai.shared_policy import policy
            if item["id"] not in {rule["id"] for rule in (await policy(session)).rules}:
                raise ManagementError(409, "这条规则没有对应的通用规则")
            for row in await session.scalars(select(Rule).where(Rule.group_id == group_id, Rule.inherited_from == item["id"])):
                row.customized = False
            await session.flush()
            result = await effective_rule(session, group_id, item["id"])
            return await self.context.record(session, actor, group_id, "rule", "恢复通用规则", result)

    async def rule(self, actor, group_id, body: RuleInput, identifier="", request_id=""):
        await self.context.access.authorize(actor, group_id, write=True)
        self.validate(body)
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id)
            if previous is not None:
                return previous
            item = await effective_rule(session, group_id, identifier) if identifier else None
            if identifier and item is None:
                raise ManagementError(404, "规则不存在")
            origin = item["id"] if item and (item.get("inherited") or item.get("inherited_from")) else ""
            row = await session.scalar(select(Rule).where(Rule.group_id == group_id, Rule.inherited_from == origin)) if origin else await session.get(Rule, identifier) if identifier else None
            if row is None:
                row = Rule(id=uid(), group_id=group_id, inherited_from=origin)
                session.add(row)
            row.customized = True
            for key, value in body.model_dump().items():
                setattr(row, key, value)
            await session.flush()
            result = {**row_dict(row), "id": origin or row.id, "inherited": False}
            return await self.context.record(
                session, actor, group_id, "rule", "修改广告规则", result, request_id
            )

    @staticmethod
    def validate(body):
        if body.kind in ("block_mention", "allow_mention"):
            if not re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", body.value):
                raise ManagementError(422, "引流账号请填写 @username 或 username，不要填写网址")
        elif body.kind != "keyword":
            try:
                if not domain(body.value) or any(c in body.value for c in (" ", "/", "?", "#")):
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise ManagementError(422, "域名规则请只填写主机名，例如 example.com") from exc

    async def preview(self, actor, group_id, body: PreviewInput):
        group = await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            rules = (
                await effective_rules(session, group_id)
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
            forwarded=body.forwarded,
        )
        return {
            "reasons": list(dict.fromkeys(result["reasons"] + result["name_reasons"])),
            "name_reasons": result["name_reasons"],
            "delete_message": bool(result["reasons"] or result["name_reasons"]),
            "mute_member": bool(result["name_reasons"] or result["mute_reasons"]) and not (body.contact or result["ban_reasons"]),
            "kick_member": bool(body.contact or result["ban_reasons"]),
            "ban_member": bool(body.contact or result["ban_reasons"]),
            "notes": (["Telegram 链接发送后会核实：外群或外部私密邀请永久封禁；本群、允许链接和明确举报引用分别处理。"]
                      if any(group.settings.get(key, True) for key in ("external_group_ban", "external_invite_ban")) and extract_links(urls) else []),
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
