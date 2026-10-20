"""Shared administration operations for Telegram and the optional web console."""

import secrets
import re

from sqlalchemy import func, select

from purrai.emoji_status import DEFAULT_BLOCKED_PACKS
from purrai.models import (
    Action,
    Group,
    Inbox,
    Member,
    Meta,
    Rule,
    Verification,
    Violation,
    Whitelist,
    now,
    uid,
)
from purrai.rules import assess, domain, message_content
from purrai.schemas import (
    ActionInput,
    AdminInput,
    EmojiPackChange,
    GroupSettings,
    PreviewInput,
    RuleInput,
    SettingsPatch,
    WhitelistInput,
)
from purrai.service import ACTIVE, enqueue, row_dict
from purrai.telegram import is_admin


class ManagementError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Management:
    def __init__(self, db, telegram, service, settings):
        self.db, self.tg, self.service, self.settings = db, telegram, service, settings

    async def archived_messages(self, actor, group_id, filters, page_number=1, size=20):
        from purrai.archive import page

        await self.authorize(actor, group_id)
        return await page(self.db, group_id, filters, page_number, size)

    async def archived_message(self, actor, group_id, identifier):
        from purrai.archive import detail

        await self.authorize(actor, group_id)
        result = await detail(self.db, group_id, identifier)
        if result is None:
            raise ManagementError(404, "消息归档不存在")
        return result

    async def archive_export(self, actor, group_id, filters, request_id):
        await self.authorize(actor, group_id)
        async with self.db.transaction() as session:
            task = await enqueue(
                session,
                "archive_export",
                f"archive-export:{actor}:{request_id}",
                group_id,
                actor_id=actor,
                reason="导出群消息归档",
                payload={"filters": filters.model_dump(), "cutoff": now()},
            )
            return row_dict(task)

    async def principal(self, actor: int, root=False) -> bool:
        owner = actor in self.settings.superadmin_ids
        if not owner and root:
            raise ManagementError(403, "此操作仅限超级管理员")
        if not owner and not await self.groups(actor):
            raise ManagementError(403, "你目前不是机器人已加入群的管理员，请先在 Telegram 群中设置管理员身份")
        return owner

    async def authorize(self, actor: int, group_id: int, write=False, root=False) -> Group:
        owner = actor in self.settings.superadmin_ids
        if root:
            await self.principal(actor, root=True)
        async with self.db.sessions() as session:
            group = await session.get(Group, group_id)
        if not group:
            raise ManagementError(404, "群不存在，请先将机器人加入群")
        if not group.approved:
            raise ManagementError(403, "此群尚未获准接入，请联系机器人所有者批准")
        if not owner:
            try:
                member = await self.tg.call("get_chat_member", chat_id=group_id, user_id=actor)
            except Exception as exc:
                raise ManagementError(503, "暂时无法核实 Telegram 群管理员身份") from exc
            if not is_admin(member):
                raise ManagementError(403, "你不是此群的 Telegram 管理员，无法管理此群")
        return group

    async def groups(self, actor):
        async with self.db.sessions() as session:
            rows = (await session.scalars(select(Group).order_by(Group.title, Group.id))).all()
        if actor in self.settings.superadmin_ids:
            return [row_dict(row) for row in rows]
        result, unavailable = [], False
        for row in rows:
            if not row.approved:
                continue
            try:
                member = await self.tg.call("get_chat_member", chat_id=row.id, user_id=actor)
            except Exception:
                unavailable = True
                continue
            if is_admin(member):
                result.append(row_dict(row))
        if not result and unavailable:
            raise ManagementError(503, "暂时无法核实 Telegram 群管理员身份，请稍后重试")
        return result

    async def group_access(self, actor, group_id, allow):
        await self.principal(actor, root=True)
        return await self.set_group_access(group_id, allow, actor)

    async def set_group_access(self, group_id, allow, actor=None):
        """The authenticated owner and local CLI share this one mutation."""
        from purrai.models import ChatTurn

        async with self.db.transaction() as session:
            group = await session.get(Group, group_id)
            if group_id >= 0:
                raise ManagementError(422, "请填写 Telegram 群的负数 ID")
            if group is None and allow:
                group = Group(id=group_id, title=str(group_id), type="supergroup", approved=False, settings=GroupSettings().model_dump())
                session.add(group)
                await session.flush()
            if not group:
                raise ManagementError(404, "群不存在，请先将机器人加入该群")
            group.approved = allow
            if not allow:
                group.enabled, group.status = False, "unapproved"
                group.settings = {**group.settings, "chat_enabled": False}
                for action in await session.scalars(select(Action).where(Action.group_id == group_id, Action.state == "pending")):
                    if not action.dedupe_key.startswith("cleanup:"):
                        action.state, action.error = "cancelled", "所有者已撤销此群接入"
                for turn in await session.scalars(select(ChatTurn).where(ChatTurn.group_id == group_id, ChatTurn.state.in_(("pending", "generating")))):
                    turn.state, turn.error = "skipped", "所有者已撤销此群接入"
                for verification in await session.scalars(select(Verification).where(Verification.group_id == group_id, Verification.state.in_(ACTIVE))):
                    verification.state, verification.error = "cancelled", "所有者已撤销此群接入"
                    await self.service.cleanup(session, verification)
            elif group.status == "unapproved":
                group.status = "pending"
            return await self.record(session, actor, group_id, "group_access", "批准群接入" if allow else "撤销群接入", row_dict(group))

    async def previous(self, session, actor, request_id):
        if not request_id:
            return None
        row = await session.scalar(select(Action).where(Action.dedupe_key == f"change:{actor}:{request_id}"))
        return row.payload["result"] if row else None

    async def record(self, session, actor, group_id, kind, reason, result, request_id=""):
        row = await enqueue(
            session,
            kind,
            f"change:{actor}:{request_id or uid()}",
            group_id,
            actor_id=actor,
            reason=reason,
            payload={"result": result},
        )
        row.state = "succeeded"
        return result

    async def permissions(self, actor, group_id, activate=False):
        group = await self.authorize(actor, group_id, write=True)
        if activate and group.type != "supergroup":
            raise ManagementError(422, "成员禁言需要 Telegram 超级群，请先升级群组")
        try:
            permissions = await self.service.check_permissions(
                group_id, group.settings.get("join_requests", False)
            )
        except Exception as exc:
            raise ManagementError(503, "无法检查机器人群权限，请确认机器人已加入群并能连接 Telegram") from exc
        async with self.db.transaction() as session:
            group = await session.get(Group, group_id)
            group.permissions = permissions
            if activate and permissions["ok"]:
                if not group.enabled:
                    group.activated_at = now()
                group.enabled = True
                await self.record(session, actor, group_id, "activate", "启用群保护", {"ok": True})
            group.status = (
                "active"
                if permissions["ok"] and group.enabled
                else ("pending" if permissions["ok"] else "error")
            )
        if activate and not permissions["ok"]:
            raise ManagementError(422, "机器人缺少权限：" + "、".join(permissions["missing"]))
        return {"ok": permissions["ok"], "permissions": permissions, **permissions}

    async def settings_patch(self, actor, group_id, body: SettingsPatch, request_id=""):
        current = await self.authorize(actor, group_id, write=True)
        if body.settings and "emoji_status_blocked_packs" in body.settings and body.settings["emoji_status_blocked_packs"] != current.settings.get(
            "emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS)
        ):
            await self.validate_status_packs(body.settings["emoji_status_blocked_packs"])
        if body.settings and body.settings.get("join_requests"):
            await self.require_join_right(group_id)
        if body.enabled is True:
            raise ManagementError(422, "启用保护请使用群接入检查操作")
        async with self.db.transaction() as session:
            previous = await self.previous(session, actor, request_id)
            if previous is not None:
                return previous
            group = await session.get(Group, group_id)
            if body.settings and body.baseline is not None:
                current_values = GroupSettings.model_validate(group.settings).model_dump()
                conflicts = [key for key, value in body.settings.items()
                             if key not in body.baseline or current_values[key] not in (body.baseline[key], value)]
                if conflicts:
                    raise ManagementError(409, "这些设置已被其他管理员修改，请重新打开群设置后再保存：" + "、".join(conflicts))
            if body.enabled is False:
                group.enabled, group.status = False, "paused"
            if body.settings:
                group.settings = GroupSettings.model_validate({**group.settings, **body.settings}).model_dump()
            return await self.record(
                session, actor, group_id, "settings", "修改群配置", row_dict(group), request_id
            )

    async def setting(self, actor, group_id, key, value, request_id):
        await self.authorize(actor, group_id, write=True)
        if key == "emoji_status_blocked_packs":
            value = GroupSettings(emoji_status_blocked_packs=value).emoji_status_blocked_packs
            await self.validate_status_packs(value)
        if key == "join_requests" and value:
            await self.require_join_right(group_id)
        async with self.db.transaction() as session:
            previous = await self.previous(session, actor, request_id)
            if previous is not None:
                return previous
            group = await session.get(Group, group_id)
            if key not in GroupSettings.model_fields:
                raise ManagementError(422, "配置项不存在")
            # Merge under the same lock, so Telegram edits cannot overwrite another admin's web changes.
            group.settings = GroupSettings.model_validate({**group.settings, key: value}).model_dump()
            return await self.record(
                session, actor, group_id, "settings", f"Telegram 修改配置：{key}", row_dict(group), request_id
            )

    async def change_status_packs(self, actor, group_id, body: EmojiPackChange):
        group = await self.authorize(actor, group_id, write=True)
        async with self.db.sessions() as session:
            previous = await self.previous(session, actor, body.request_id)
        if previous is not None:
            return self.status_pack_replay(previous, group_id, body)
        if body.operation == "add":
            current = group.settings.get("emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS))
            await self.validate_status_packs([pack for pack in body.packs if pack not in current])
        async with self.db.transaction() as session:
            previous = await self.previous(session, actor, body.request_id)
            if previous is not None:
                return self.status_pack_replay(previous, group_id, body)
            group = await session.get(Group, group_id)
            if not group or not group.approved:
                raise ManagementError(403, "此群尚未获准接入")
            current = group.settings.get("emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS))
            if body.operation == "add":
                updated = list(dict.fromkeys([*current, *body.packs]))
            elif body.operation == "remove":
                updated = [pack for pack in current if pack not in body.packs]
            else:
                updated = []
            if len(updated) > 100:
                raise ManagementError(422, "每群最多添加 100 个状态表情包，请先移除不再需要的条目")
            group.settings = {**group.settings, "emoji_status_blocked_packs": updated}
            result = {
                "group_id": group_id,
                "operation": body.operation,
                "requested_packs": body.packs,
                "packs": updated,
                "added": [pack for pack in updated if pack not in current],
                "removed": [pack for pack in current if pack not in updated],
            }
            return await self.record(
                session, actor, group_id, "settings",
                {"add": "新增", "remove": "移除", "clear": "清空"}[body.operation] + "状态表情包黑名单",
                result, body.request_id,
            )

    @staticmethod
    def status_pack_replay(previous, group_id, body):
        if (
            previous.get("group_id") != group_id
            or previous.get("operation") != body.operation
            or previous.get("requested_packs") != body.packs
        ):
            raise ManagementError(409, "请求标识已用于其他操作")
        return previous

    async def validate_status_packs(self, packs):
        from aiogram.exceptions import TelegramBadRequest

        for name in packs:
            try:
                result = await self.tg.call("get_sticker_set", name=name, request_timeout=3)
            except TelegramBadRequest as exc:
                raise ManagementError(422, f"表情包不存在或无法读取：{name}") from exc
            except Exception as exc:
                raise ManagementError(503, "暂时无法向 Telegram 核实表情包，请稍后重试") from exc
            if result.get("sticker_type") != "custom_emoji" or result.get("name", "").lower() != name:
                raise ManagementError(422, f"请使用自定义表情包的 addemoji 链接：{name}")

    async def chat_status(self, actor, group_id):
        await self.authorize(actor, group_id)
        return await self.chat.status(group_id)

    async def reset_chat(self, actor, group_id, request_id):
        from sqlalchemy import update
        from purrai.models import ChatTurn

        await self.authorize(actor, group_id, write=True)
        async with self.db.transaction() as session:
            previous = await self.previous(session, actor, request_id)
            if previous is not None:
                return previous
            key = f"chat_reset:{group_id}"
            row = await session.get(Meta, key)
            if row:
                row.value = str(now())
            else:
                session.add(Meta(key=key, value=str(now())))
            await session.execute(
                update(ChatTurn)
                .where(
                    ChatTurn.group_id == group_id,
                    ChatTurn.state.in_(("pending", "generating")),
                )
                .values(state="skipped")
            )
            return await self.record(session, actor, group_id, "settings", "重新开始群聊天", {}, request_id)

    async def verification_action(self, actor, identifier, body: ActionInput):
        async with self.db.sessions() as session:
            row = await session.get(Verification, identifier)
        if not row:
            raise ManagementError(404, "验证记录不存在")
        await self.authorize(actor, row.group_id, write=True)
        async with self.db.transaction() as session:
            existing = await session.scalar(
                select(Action).where(Action.dedupe_key == f"manual:{actor}:{body.request_id}")
            )
            if existing:
                expected = ("challenge", "verify_mute") if body.action == "reissue" else (body.action,)
                if (
                    existing.payload.get("verification_id") or existing.payload.get("source_verification_id")
                ) != identifier or existing.kind not in expected:
                    raise ManagementError(409, "请求标识已用于其他操作")
                return row_dict(existing)
            row = await session.get(Verification, identifier)
            retry_muted = (
                row.state == "failed"
                and row.failure_action == "permanent_mute"
                and body.action in ("release", "reissue")
            )
            if row.state not in ACTIVE and body.action != "unban" and not retry_muted:
                raise ManagementError(409, "此验证已经结束，请刷新记录")
            if body.action not in ("release", "reissue", "ban", "unban"):
                raise ManagementError(422, "此记录不支持该操作")
            member = await session.get(Member, (row.group_id, row.user_id))
            if retry_muted and (
                not member
                or member.generation != row.generation
                or not member.present
                or not member.owned_restriction
                or member.owned_restriction.get("verification_id") != row.id
            ):
                raise ManagementError(409, "该成员已离群、重新入群或限制状态已变化，请刷新记录")
            row.nonce, row.input_digits = secrets.token_hex(4), ""
            if body.action == "reissue":
                group = await session.get(Group, row.group_id)
                row.deadline = now() + group.settings["verification_timeout"]
                if row.flow == "join_request":
                    row.deadline = min(row.deadline, row.request_date + 240)
                    if row.deadline <= now():
                        raise ManagementError(409, "申请私聊窗口已过期，请让用户重新申请入群")
                row.state = "muting"
                kind = "verify_mute" if row.flow == "joined" else "challenge"
            else:
                kind = body.action
                if kind != "unban":
                    row.state = "releasing" if kind == "release" else "removing"
            action = await enqueue(
                session,
                kind,
                f"manual:{actor}:{body.request_id}",
                row.group_id,
                row.user_id,
                {"verification_id": identifier}
                if kind != "unban"
                else {"source_verification_id": identifier, "request_cooldown": row.flow == "join_request"},
                0,
                actor,
                body.reason,
            )
            return row_dict(action)

    async def require_join_right(self, group_id):
        try:
            permissions = await self.service.check_permissions(group_id, True)
        except Exception as exc:
            raise ManagementError(503, "暂时无法检查申请入群权限") from exc
        if not permissions["ok"]:
            raise ManagementError(422, "申请入群验证缺少机器人权限：" + "、".join(permissions["missing"]))

    async def join_link(self, actor, group_id, request_id):
        group = await self.authorize(actor, group_id, write=True)
        if not group.enabled or not group.settings.get("join_requests"):
            raise ManagementError(409, "请先启用群保护和申请入群验证")
        await self.require_join_right(group_id)
        async with self.db.transaction() as session:
            row = await enqueue(
                session,
                "join_link",
                f"join-link:{actor}:{request_id}",
                group_id,
                priority=5,
                actor_id=actor,
                reason="生成需要审核的入群链接",
            )
            return row_dict(row)

    async def release_pending(self, actor, group_id, body: ActionInput):
        await self.authorize(actor, group_id, write=True)
        if body.action != "release":
            raise ManagementError(422, "批量操作只支持放行")
        async with self.db.transaction() as session:
            key = f"batch:{actor}:{body.request_id}"
            existing = await session.scalar(select(Action).where(Action.dedupe_key == key))
            if existing:
                if existing.group_id != group_id:
                    raise ManagementError(409, "请求标识已用于其他群")
                return {"action_ids": existing.payload["action_ids"]}
            rows = (
                await session.scalars(
                    select(Verification).where(
                        Verification.group_id == group_id, Verification.state.in_(ACTIVE)
                    )
                )
            ).all()
            identifiers = []
            for row in rows:
                row.state, row.nonce = "releasing", secrets.token_hex(4)
                action = await enqueue(
                    session,
                    "release",
                    f"{key}:{row.id}",
                    group_id,
                    row.user_id,
                    {"verification_id": row.id},
                    0,
                    actor,
                    body.reason,
                )
                identifiers.append(action.id)
            batch = await enqueue(
                session,
                "batch_release",
                key,
                group_id,
                actor_id=actor,
                reason=body.reason,
                payload={"action_ids": identifiers},
            )
            batch.state = "succeeded"
            return {"action_ids": identifiers}

    async def rule(self, actor, group_id, body: RuleInput, identifier="", request_id=""):
        await self.authorize(actor, group_id, write=True)
        if body.kind in ("block_mention", "allow_mention"):
            if not re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", body.value):
                raise ManagementError(422, "引流账号请填写 @username 或 username，不要填写网址")
        elif body.kind != "keyword":
            try:
                if not domain(body.value) or any(c in body.value for c in (" ", "/", "?", "#")):
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise ManagementError(422, "域名规则请只填写主机名，例如 example.com") from exc
        async with self.db.transaction() as session:
            previous = await self.previous(session, actor, request_id)
            if previous is not None:
                return previous
            row = await session.get(Rule, identifier) if identifier else Rule(id=uid(), group_id=group_id)
            if not row or row.group_id != group_id:
                raise ManagementError(404, "规则不存在")
            if not identifier:
                session.add(row)
            for key, value in body.model_dump().items():
                setattr(row, key, value)
            return await self.record(
                session, actor, group_id, "rule", "修改广告规则", row_dict(row), request_id
            )

    async def preview(self, actor, group_id, body: PreviewInput):
        group = await self.authorize(actor, group_id)
        async with self.db.sessions() as session:
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
            "mute_member": bool(result["name_reasons"]),
        }

    async def whitelist(self, actor, group_id, body: WhitelistInput, request_id="", remove=False):
        await self.authorize(actor, group_id, write=True)
        async with self.db.transaction() as session:
            previous = await self.previous(session, actor, request_id)
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
            return await self.record(
                session,
                actor,
                group_id,
                "whitelist",
                "移出白名单" if remove else "修改白名单",
                result,
                request_id,
            )

    async def admin(self, actor, body: AdminInput, request_id=""):
        await self.principal(actor, root=True)
        raise ManagementError(
            410, "群管理权限已改为自动识别，请直接在 Telegram 群中设置或撤销管理员；旧授权记录不再决定权限"
        )

    async def violation_action(self, actor, group_id, identifier, body: ActionInput):
        await self.authorize(actor, group_id, write=True)
        async with self.db.transaction() as session:
            row = await session.get(Violation, identifier)
            if not row or row.group_id != group_id:
                raise ManagementError(404, "违规记录不存在")
            if body.action not in ("unmute", "unban", "revoke_warning") or row.sender_chat:
                raise ManagementError(422, "此记录不支持该纠正操作")
            existing = await session.scalar(
                select(Action).where(Action.dedupe_key == f"correction:{actor}:{body.request_id}")
            )
            if existing:
                if existing.payload.get("violation_id") != identifier or existing.kind != body.action:
                    raise ManagementError(409, "请求标识已用于其他操作")
                return row_dict(existing)
            action = await enqueue(
                session,
                body.action,
                f"correction:{actor}:{body.request_id}",
                group_id,
                row.user_id,
                {"violation_id": row.id},
                0,
                actor,
                body.reason,
            )
            if body.action == "revoke_warning":
                row.warning_active, action.state = False, "succeeded"
            return row_dict(action)

    async def member_action(self, actor, group_id, user_id, action, request_id, reason="Telegram 管理员操作"):
        await self.authorize(actor, group_id, write=True)
        if user_id <= 0 or action not in ("mute", "ban", "unmute", "unban", "release", "reissue"):
            raise ManagementError(422, "请选择有效的成员和操作")
        async with self.db.sessions() as session:
            existing = await session.scalar(
                select(Action).where(
                    Action.dedupe_key.in_((f"manual:{actor}:{request_id}", f"member:{actor}:{request_id}"))
                )
            )
            if existing:
                kinds = {
                    "mute": ("manual_mute",),
                    "ban": ("ban", "manual_ban"),
                    "unmute": ("unmute", "release"),
                    "unban": ("unban",),
                    "release": ("release",),
                    "reissue": ("challenge", "verify_mute"),
                }
                if (
                    existing.group_id != group_id
                    or existing.user_id != user_id
                    or existing.kind not in kinds[action]
                ):
                    raise ManagementError(409, "请求标识已用于其他操作")
                return row_dict(existing)
            current_member = await session.get(Member, (group_id, user_id))
            verification = await session.scalar(
                select(Verification)
                .where(
                    Verification.group_id == group_id,
                    Verification.user_id == user_id,
                    Verification.generation == current_member.generation if current_member else False,
                    (Verification.state.in_(ACTIVE))
                    | ((Verification.state == "failed") & (Verification.failure_action == "permanent_mute")),
                )
                .order_by(Verification.created_at.desc())
            )
        if verification and verification.state == "failed" and action not in ("release", "reissue", "unmute"):
            verification = None
        if verification and verification.state in ACTIVE and action == "mute":
            raise ManagementError(409, "此成员正在验证，已处于禁言状态；请使用放行、重出题或封禁")
        if verification and action in ("release", "reissue", "ban", "unmute"):
            return await self.verification_action(
                actor,
                verification.id,
                ActionInput(
                    action="release" if action == "unmute" else action, reason=reason, request_id=request_id
                ),
            )
        if action in ("release", "reissue"):
            raise ManagementError(409, "此成员没有正在进行的验证")
        async with self.db.transaction() as session:
            key = f"member:{actor}:{request_id}"
            existing = await session.scalar(select(Action).where(Action.dedupe_key == key))
            kind = {"mute": "manual_mute", "ban": "manual_ban"}.get(action, action)
            if existing:
                if existing.group_id != group_id or existing.user_id != user_id or existing.kind != kind:
                    raise ManagementError(409, "请求标识已用于其他操作")
                return row_dict(existing)
            member = await session.get(Member, (group_id, user_id))
            if member is None:
                member = Member(group_id=group_id, user_id=user_id)
                session.add(member)
                await session.flush()
            row = await enqueue(
                session, kind, key, group_id, user_id, {"generation": member.generation}, 0, actor, reason
            )
            return row_dict(row)

    async def records(self, actor, group_id, model, page=0, pending=False):
        await self.authorize(actor, group_id)
        filters = [model.group_id == group_id]
        if pending and model is Verification:
            filters.append(Verification.state.in_(ACTIVE))
        order = (
            model.created_at.desc()
            if hasattr(model, "created_at")
            else model.user_id
            if model is Whitelist
            else model.id
        )
        async with self.db.sessions() as session:
            count = await session.scalar(select(func.count()).select_from(model).where(*filters))
            rows = (
                await session.scalars(select(model).where(*filters).order_by(order).offset(page * 5).limit(5))
            ).all()
            result = [row_dict(row) for row in rows]
            for row in result:
                for key in ("answer_hash", "input_digits", "nonce"):
                    row.pop(key, None)
            return result, count

    async def track(self, actor, group_id, chat_id, action_ids, request_id):
        await self.authorize(actor, group_id)
        if not action_ids:
            return
        async with self.db.transaction() as session:
            await enqueue(
                session,
                "admin_notify",
                f"notify:{actor}:{request_id}",
                group_id,
                payload={"chat_id": chat_id, "action_ids": action_ids},
                priority=20,
            )

    async def backup(self, actor, request_id=""):
        await self.principal(actor, root=True)
        async with self.db.sessions() as session:
            previous = await self.previous(session, actor, request_id)
            if previous is not None:
                return previous
        path = await self.db.backup(self.settings.backup_dir)
        async with self.db.transaction() as session:
            row = await session.get(Meta, "last_backup")
            if row:
                row.value = str(path)
            else:
                session.add(Meta(key="last_backup", value=str(path)))
            return await self.record(
                session,
                actor,
                None,
                "backup",
                "生成一致性数据库备份",
                {"file": path.name, "created_at": now()},
                request_id,
            )

    async def retry(self, actor, identifier, event=False, request_id=""):
        await self.principal(actor, root=True)
        async with self.db.transaction() as session:
            previous = await self.previous(session, actor, request_id)
            if previous is not None:
                return previous
            row = await session.get(Inbox if event else Action, identifier)
            if not row or row.state != "failed":
                raise ManagementError(409, "此事件当前不能重试" if event else "此任务当前不能重试")
            row.state, row.attempts, row.available_at, row.error = "pending", 0, now(), ""
            return await self.record(
                session,
                actor,
                None if event else row.group_id,
                "retry_event" if event else "retry",
                "重试失败事件" if event else "重试失败任务",
                {"ok": True, "event_id" if event else "action_id": identifier},
                request_id,
            )
