"""Administration operations for rule admin."""

import asyncio
import re

from sqlalchemy import select

from purrai.errors import ManagementError
from purrai.models import (
    Rule,
    Whitelist,
    uid,
)
from purrai.rules import CONTACT_REASON, ai_keyword_rules, assess, domain, message_content
from purrai.telegram_links import extract_links
from purrai.schemas import (
    AdminInput,
    PreviewInput,
    RuleInput,
    WhitelistInput,
)
from purrai.serialization import row_dict
from purrai.record_versions import check_version, versioned
from purrai.shared_policy import effective_rule, effective_rules
from purrai.rule_presentation import describe_rule
from purrai.business_rules import with_rules


class RuleAdministration:
    def __init__(self, context):
        self.context = context

    async def inherit(self, actor, group_id, identifier, expected_version=None, request_id=""):
        await self.context.access.authorize(actor, group_id, write=True)
        async with self.context.access.group_transaction(group_id) as session:
            intent = {"operation": "inherit", "group_id": group_id, "id": identifier, "version": expected_version}
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            item = await effective_rule(session, group_id, identifier)
            if item is None:
                raise ManagementError(404, "规则不存在")
            check_version(item, expected_version)
            from purrai.shared_policy import policy
            if item["id"] not in {rule["id"] for rule in (await policy(session)).rules}:
                raise ManagementError(409, "这条规则没有对应的通用规则")
            for row in await session.scalars(select(Rule).where(Rule.group_id == group_id, Rule.inherited_from == item["id"])):
                row.customized = False
            await session.flush()
            result = await effective_rule(session, group_id, item["id"])
            return await self.context.record(session, actor, group_id, "rule", "恢复通用规则", result, request_id, intent)

    async def rule(self, actor, group_id, body: RuleInput, identifier="", request_id="", expected_version=None):
        return await self._save_rule(actor, group_id, body, identifier, request_id, expected_version)

    async def patch_rule(self, actor, group_id, identifier, changes, request_id, expected_version):
        if not identifier or not changes or changes.keys() - RuleInput.model_fields.keys():
            raise ManagementError(422, "规则修改字段不存在")
        return await self._save_rule(actor, group_id, None, identifier, request_id, expected_version, changes)

    async def _save_rule(self, actor, group_id, body, identifier, request_id, expected_version, changes=None):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "rule_patch" if changes is not None else "rule", "group_id": group_id,
                  "id": identifier, "body": changes if changes is not None else body.model_dump(),
                  "version": expected_version}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            item = await effective_rule(session, group_id, identifier) if identifier else None
            if identifier and item is None:
                raise ManagementError(404, "规则不存在")
            if identifier:
                check_version(item, expected_version)
            if changes is not None:
                body = RuleInput(**{**item, **changes})
            self.validate(body)
            origin = item["id"] if item and (item.get("inherited") or item.get("inherited_from")) else ""
            row = await session.scalar(select(Rule).where(Rule.group_id == group_id, Rule.inherited_from == origin)) if origin else await session.get(Rule, identifier) if identifier else None
            if row is None:
                row = Rule(id=uid(), group_id=group_id, inherited_from=origin)
                session.add(row)
            row.customized = True
            for key, value in body.model_dump().items():
                setattr(row, key, value)
            await session.flush()
            result = describe_rule(await effective_rule(session, group_id, origin or row.id))
            return await self.context.record(
                session, actor, group_id, "rule", "修改广告规则", result, request_id, intent
            )

    @staticmethod
    def validate(body):
        if body.kind == "block_target":
            from purrai.promotion_targets import normalize_target
            target = normalize_target(body.value)
            if not target:
                raise ManagementError(422, "请填写推广账号、Telegram 链接、频道 ID、手机号、邮箱或网站域名")
            body.value = target
        elif body.kind in ("block_mention", "allow_mention"):
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
            rules = await effective_rules(session, group_id)
            if body.rules is not None:
                rules = with_rules(rules, [r.model_dump() for r in body.rules])
        if body.draft is not None:
            self.validate(body.draft)
            rules = with_rules(rules, [rule for rule in rules if rule.get("id") != body.draft.id] + [body.draft.model_dump()])
        text, urls = message_content({"text": body.text})
        from purrai.solicitation_rules import contact_should_block
        contact = body.contact and contact_should_block({"text": text, "contact": {"first_name": body.contact_name}}, group.settings, rules)
        result = await asyncio.to_thread(assess,
            text,
            urls,
            rules,
            group.settings,
            body.newcomer,
            contact,
            body.repeats,
            body.similar,
            body.burst,
            body.sender_name,
            forwarded=body.forwarded,
            inline_buttons=body.inline_buttons,
        )
        conditional = ai_keyword_rules(text, rules, body.sender_name)
        return {
            "reasons": list(dict.fromkeys(result["reasons"] + result["name_reasons"])),
            "name_reasons": result["name_reasons"],
            "delete_message": bool(result["reasons"] or result["name_reasons"]),
            "mute_member": bool(result["name_reasons"] or result["mute_reasons"]) and not (CONTACT_REASON in result["reasons"] or result["ban_reasons"]),
            "kick_member": bool(CONTACT_REASON in result["reasons"] or result["ban_reasons"]),
            "ban_member": bool(CONTACT_REASON in result["reasons"] or result["ban_reasons"]),
            "ai_confirmation_required": bool(conditional),
            "notes": (["Telegram 链接发送后会核实：外群或外部私密邀请永久封禁；本群、允许链接和明确举报引用分别处理。"]
                      if any(group.settings.get(key, True) for key in ("external_group_ban", "external_invite_ban")) and extract_links(urls) else [])
                     + (["命中AI确认关键词：" + "、".join(rule["value"] for rule in conditional)
                         + "。仅在原AI调用条件满足且模型确认广告后，删除并永久禁言；本次预览未调用AI。"] if conditional else []),
        }

    async def whitelist(self, actor, group_id, body: WhitelistInput, request_id="", remove=False, expected_version=None):
        return await self._save_whitelist(actor, group_id, body, request_id, remove, expected_version)

    async def patch_whitelist(self, actor, group_id, user_id, changes, request_id, expected_version):
        if not changes or changes.keys() - (WhitelistInput.model_fields.keys() - {"user_id"}):
            raise ManagementError(422, "白名单修改字段不存在")
        return await self._save_whitelist(
            actor, group_id, WhitelistInput(user_id=user_id), request_id, False, expected_version, changes,
        )

    async def _save_whitelist(self, actor, group_id, body, request_id, remove, expected_version, changes=None):
        await self.context.access.authorize(actor, group_id, write=True)
        operation = "whitelist_patch" if changes is not None else "whitelist_remove" if remove else "whitelist"
        intent = {"operation": operation, "group_id": group_id, "body": body.model_dump(),
                  "version": expected_version, **({"changes": changes} if changes is not None else {})}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            row = await session.get(Whitelist, (group_id, body.user_id))
            if changes is not None and row is None:
                raise ManagementError(404, "白名单记录不存在，请刷新列表")
            # New records need no prior read for internal bot creation; existing
            # records always require the version observed by the editor.
            if row is not None or expected_version is not None:
                check_version(row_dict(row) if row else None, expected_version)
            if changes is not None:
                body = WhitelistInput(**{**row_dict(row), **changes})
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
                result = versioned(row_dict(row))
            return await self.context.record(
                session,
                actor,
                group_id,
                "whitelist",
                "移出白名单" if remove else "修改白名单",
                result,
                request_id,
                intent,
            )

    async def admin(self, actor, body: AdminInput, request_id=""):
        await self.context.access.principal(actor, root=True)
        raise ManagementError(
            410, "群管理权限已改为自动识别，请直接在 Telegram 群中设置或撤销管理员；旧授权记录不再决定权限"
        )
