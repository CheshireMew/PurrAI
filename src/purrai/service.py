import asyncio
import logging
import secrets

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, select

from purrai import captcha
from purrai.emoji_status import EmojiStatusGuard
from purrai.media_gate import remember_text, text_count_before, visual_media
from purrai.archive import capture, deletion_result, mark_reasons
from purrai.models import (
    Action,
    Fingerprint,
    Group,
    Member,
    Meta,
    Rule,
    Verification,
    Violation,
    Whitelist,
    default_settings,
    now,
    uid,
)
from purrai.rules import assess, content_digest, is_similar, message_content, similarity_signature
from purrai.telegram import ALL_PERMISSIONS, is_admin, is_present, muted_permissions, permission_snapshot
from purrai.verification_progress import VerificationProgress
from purrai.action_execution import ActionExecution, ActionSuperseded
from purrai.diagnostics import failure

logger = logging.getLogger(__name__)
ACTIVE = ("muting", "pending", "releasing", "removing", "failing")
AUTOMATIC_MEMBER_ACTIONS = (
    "verify_mute", "challenge", "progress", "verification_failure", "ban", "release",
    "profile_mute", "emoji_status_check", "emoji_status_mute", "moderation_mute", "delete", "warn",
)


async def enqueue(
    session,
    kind: str,
    key: str,
    group_id=None,
    user_id=None,
    payload=None,
    priority=10,
    actor_id=None,
    reason="",
) -> Action:
    existing = await session.scalar(select(Action).where(Action.dedupe_key == key))
    if existing:
        return existing
    payload = dict(payload or {})
    if payload.get("verification_id"):
        verification = await session.get(Verification, payload["verification_id"])
        if verification:
            payload.setdefault("expected_nonce", verification.nonce)
    item = Action(
        id=uid(),
        kind=kind,
        dedupe_key=key,
        group_id=group_id,
        user_id=user_id,
        payload=payload,
        priority=priority,
        actor_id=actor_id,
        reason=reason,
        state="pending",
        attempts=0,
        available_at=now(),
        error="",
    )
    session.add(item)
    await session.flush()
    if kind == "delete":
        await deletion_result(session, item)
    return item


async def audit(session, kind: str, actor_id: int, group_id=None, reason="", payload=None):
    item = await enqueue(
        session, kind, uid(), group_id=group_id, actor_id=actor_id, reason=reason, payload=payload
    )
    item.state = "succeeded"
    return item


class Service:
    def __init__(self, db, telegram, settings):
        self.db, self.tg, self.settings = db, telegram, settings
        self.action_lock = asyncio.Lock()
        self.progress = VerificationProgress(db, telegram, settings, self.exempt_administrator)
        self.emoji_status = EmojiStatusGuard(db, telegram, settings, enqueue, self.exempt_administrator)

    @staticmethod
    def administrator_protected_action(action):
        return bool(
            action.actor_id is None and action.group_id and action.user_id and action.user_id > 0
            and action.kind in AUTOMATIC_MEMBER_ACTIONS and not action.dedupe_key.startswith("cleanup:")
        )

    async def exempt_administrator(self, session, group_id, user_id):
        """Apply a confirmed current Telegram role without persisting a whitelist."""
        for action in await session.scalars(select(Action).where(
            Action.group_id == group_id, Action.user_id == user_id, Action.state == "pending",
            Action.actor_id.is_(None), Action.kind.in_(AUTOMATIC_MEMBER_ACTIONS),
        )):
            if self.administrator_protected_action(action):
                action.state, action.error, action.updated_at = "cancelled", "群主或管理员免自动限制", now()
                await deletion_result(session, action)
        for verification in await session.scalars(select(Verification).where(
            Verification.group_id == group_id, Verification.user_id == user_id,
            Verification.state.in_(ACTIVE),
        )):
            verification.state, verification.error, verification.failure_action = "passed", "", ""
            verification.nonce, verification.updated_at = secrets.token_hex(4), now()
            await self.cleanup(session, verification)
        member = await session.get(Member, (group_id, user_id))
        if member:
            member.passed_at, member.request_until, member.present = now(), 0, True
            member.owned_restriction, member.restriction_conflict = None, False

    async def check_permissions(self, group_id: int, join_requests=False) -> dict:
        member = await self.tg.call("get_chat_member", chat_id=group_id, user_id=self.tg.user_id)
        missing = [
            name
            for name in ("can_delete_messages", "can_restrict_members")
            if not member.get(name) and member.get("status") != "creator"
        ]
        if not is_admin(member):
            missing.append("administrator")
        if join_requests and not member.get("can_invite_users") and member.get("status") != "creator":
            missing.append("can_invite_users")
        return {"ok": not missing, "missing": missing, "checked_at": now()}

    async def member_event(self, event: dict, bot_event=False, event_id=0):
        chat = event["chat"]
        from purrai.private_access import approved, discover

        if bot_event:
            async with self.db.transaction() as session:
                group = await discover(session, chat, self.settings.pending_group_limit)
                if not group or not group.approved:
                    return
        else:
            async with self.db.sessions() as session:
                if not await approved(session, chat["id"]):
                    return
        old, new = event["old_chat_member"], event["new_chat_member"]
        actor = event.get("from", {})
        manual_unmute = bool(
            not bot_event
            and old.get("status") == "restricted"
            and not old.get("can_send_messages", False)
            and is_present(new)
            and (
                new.get("status") == "member"
                or new.get("status") == "restricted"
                and new.get("can_send_messages")
            )
            and actor.get("id")
            and actor["id"] != self.tg.user_id
            and not actor.get("is_bot")
        )
        if manual_unmute and actor["id"] not in self.settings.superadmin_ids:
            # A human administrator's explicit event grants trust; automatic
            # expiry and another bot's changes must never create a whitelist.
            manual_unmute = is_admin(
                await self.tg.call("get_chat_member", chat_id=chat["id"], user_id=actor["id"])
            )
        async with self.db.transaction() as session:
            group = await session.get(Group, chat["id"])
            if not group:
                group = Group(
                    id=chat["id"],
                    title=chat.get("title", ""),
                    type=chat["type"],
                    settings=default_settings(),
                    enabled=False,
                    status="pending",
                    activated_at=0,
                )
                session.add(group)
                await session.flush()
            group.title = chat.get("title", group.title)
            old, new = event["old_chat_member"], event["new_chat_member"]
            if bot_event:
                group.status = "active" if group.enabled and is_admin(new) else "pending"
                if not is_present(new) or (
                    group.enabled
                    and not (
                        new.get("can_delete_messages")
                        and new.get("can_restrict_members")
                        and (not group.settings.get("join_requests") or new.get("can_invite_users"))
                    )
                ):
                    group.status = "error"
                return
            user = new["user"]
            member = await session.get(Member, (group.id, user["id"]))
            if member and event_id and member.last_event_id >= event_id:
                return
            if member and event_id:
                member.last_event_id = event_id
            if is_admin(new):
                await self.exempt_administrator(session, group.id, user["id"])
                return
            if is_present(old) and not is_present(new):
                if member:
                    member.present = False
                    if event.get("from", {}).get("id") != self.tg.user_id:
                        member.owned_restriction = None
                    for verification in (
                        await session.scalars(
                            select(Verification).where(
                                Verification.group_id == group.id,
                                Verification.user_id == user["id"],
                                Verification.state.in_(ACTIVE),
                            )
                        )
                    ).all():
                        verification.state = "left"
                        await self.cleanup(session, verification)
                return
            if is_present(old) and is_present(new):
                if is_admin(old) and new.get("status") == "member":
                    if member:
                        member.present = True
                        member.owned_restriction, member.restriction_conflict = None, False
                    return
                if manual_unmute:
                    if member is None:
                        member = Member(group_id=group.id, user_id=user["id"], joined_at=0)
                        session.add(member)
                        await session.flush()
                    await self.trust_member(session, member, actor["id"], "Telegram 群设置手动解除禁言")
                    if event_id:
                        member.last_event_id = event_id
                    member.owned_restriction, member.restriction_conflict = None, False
                    return
                # Changes made by another admin are never silently undone by PurrAI.
                if member and event.get("from", {}).get("id") != self.tg.user_id:
                    member.restriction_conflict = True
                return
            if is_present(old) or not is_present(new):
                return
            if not group.enabled or event["date"] + 1 <= group.activated_at:
                return
            if member:
                approved = await session.scalar(
                    select(Verification).where(
                        Verification.group_id == group.id,
                        Verification.user_id == user["id"],
                        Verification.generation == member.generation,
                        Verification.flow == "join_request",
                        Verification.state.in_(("passed", "releasing")),
                    )
                )
                if approved and not approved.previous_permissions.get("entry_consumed"):
                    approved.previous_permissions = {**approved.previous_permissions, "entry_consumed": True}
                    approved.state, approved.error, approved.updated_at = "passed", "", now()
                    member.present, member.joined_at, member.passed_at = True, event["date"], now()
                    member.last_event_id, member.request_until = event_id, 0
                    await self.cleanup(session, approved)
                    pending = (
                        await session.scalars(
                            select(Action).where(
                                Action.kind == "release",
                                Action.state == "pending",
                                Action.payload["verification_id"].as_string() == approved.id,
                            )
                        )
                    ).all()
                    for action in pending:
                        action.state = "succeeded"
                    await self.emoji_status.queue(session, group, user)
                    await self.profile_on_join(session, group, member, user)
                    return
            if member and member.present and member.joined_at == event["date"]:
                return
            if member is None:
                member = Member(group_id=group.id, user_id=user["id"])
                session.add(member)
            member.joined_at = event["date"]
            member.last_event_id = event_id
            member.present = True
            member.generation = uid()
            member.passed_at = 0
            member.observed_messages = 0
            member.owned_restriction = None
            member.restriction_conflict = False
            # Invalidate any earlier join's captcha without touching current membership permissions.
            for prior in (
                await session.scalars(
                    select(Verification).where(
                        Verification.group_id == group.id,
                        Verification.user_id == user["id"],
                        Verification.state.in_(ACTIVE),
                    )
                )
            ).all():
                prior.state = "superseded"
                await self.cleanup(session, prior)
            white = await session.get(Whitelist, (group.id, user["id"]))
            if is_admin(new) or user["id"] in self.settings.superadmin_ids:
                member.passed_at = now()
                return
            if not white or not white.moderation:
                await self.emoji_status.queue(session, group, user)
            if await self.profile_on_join(session, group, member, user):
                return
            if (white and white.verification) or not group.settings["verification_enabled"]:
                member.passed_at = now()
                return
            verification = Verification(
                id=uid(),
                group_id=group.id,
                user_id=user["id"],
                user_name=user.get("first_name", str(user["id"])),
                generation=member.generation,
                state="muting",
                nonce=secrets.token_hex(4),
                attempts=0,
                input_digits="",
                deadline=now() + group.settings["verification_timeout"],
                previous_permissions=permission_snapshot(new) if new["status"] == "restricted" else {},
            )
            session.add(verification)
            await session.flush()
            await enqueue(
                session,
                "verify_mute",
                f"mute:{verification.id}",
                group.id,
                user["id"],
                {"verification_id": verification.id},
                priority=0,
            )

    async def profile_on_join(self, session, group, member, user):
        if user["id"] in self.settings.superadmin_ids:
            return False
        white = await session.get(Whitelist, (group.id, user["id"]))
        if white and white.moderation:
            return False
        profile = " ".join(
            [
                str(user.get("first_name", "")),
                str(user.get("last_name", "")),
                "@" + user["username"] if user.get("username") else "",
            ]
        ).strip()[:500]
        rules = (await session.scalars(select(Rule).where(Rule.group_id == group.id))).all()
        result = assess("", [], [row_dict(row) for row in rules], group.settings, sender_name=profile)
        if not result["name_reasons"]:
            return False
        violation = Violation(
            id=uid(),
            group_id=group.id,
            user_id=user["id"],
            message_id=-int(member.generation[:15], 16),
            sender_name=profile,
            excerpt="入群昵称检查",
            reasons=result["name_reasons"],
            profile_violation=True,
            message_violation=False,
            warning_active=False,
        )
        session.add(violation)
        await session.flush()
        await self.apply_penalty(session, group, violation, member)
        return True

    async def join_request(self, event, event_id=0):
        async with self.db.transaction() as session:
            group = await session.get(Group, event["chat"]["id"])
            if not group or not group.approved or not group.enabled or not group.settings.get("join_requests"):
                return
            user = event["from"]
            epoch = f"update:{event_id}" if event_id else f"date:{event['date']}"
            key = f"join-request:{group.id}:{user['id']}:{epoch}"
            if await session.get(Meta, key):
                return
            session.add(Meta(key=key, value="received"))
            member = await session.get(Member, (group.id, user["id"]))
            if member and event_id and member.last_event_id >= event_id:
                return
            if member is None:
                member = Member(group_id=group.id, user_id=user["id"])
                session.add(member)
            cooldown = member.request_until or 0
            prior = (
                await session.scalars(
                    select(Verification).where(
                        Verification.group_id == group.id,
                        Verification.user_id == user["id"],
                        Verification.state.in_(ACTIVE),
                    )
                )
            ).all()
            for item in prior:
                item.state = "superseded"
                await self.cleanup(session, item)
            member.generation, member.present, member.passed_at = uid(), False, 0
            member.joined_at, member.observed_messages, member.last_event_id = event["date"], 0, event_id
            member.owned_restriction, member.restriction_conflict = None, False
            white = await session.get(Whitelist, (group.id, user["id"]))
            deadline = min(now() + group.settings["verification_timeout"], event["date"] + 240)
            exempt = (
                user["id"] in self.settings.superadmin_ids
                or white
                and white.verification
                or not group.settings["verification_enabled"]
            )
            verification = Verification(
                id=uid(),
                group_id=group.id,
                user_id=user["id"],
                user_name=user.get("first_name", str(user["id"])),
                generation=member.generation,
                flow="join_request",
                prompt_chat_id=event["user_chat_id"],
                request_date=event["date"],
                deadline=deadline,
                state="removing"
                if cooldown > now() or deadline <= now()
                else "releasing"
                if exempt
                else "muting",
            )
            session.add(verification)
            await session.flush()
            if verification.state == "removing":
                verification.error = "重新申请间隔未到" if cooldown > now() else "申请已过期，请重新申请"
                kind = "ban"
            else:
                kind = "release" if exempt else "challenge"
            await enqueue(
                session, kind, f"{key}:start", group.id, user["id"], {"verification_id": verification.id}, 0
            )

    async def cleanup(self, session, verification):
        if verification.message_id:
            await enqueue(
                session,
                "delete",
                f"cleanup:{verification.id}:{verification.message_id}",
                verification.group_id,
                verification.user_id,
                {
                    "message_id": verification.message_id,
                    "chat_id": verification.prompt_chat_id or verification.group_id,
                },
                priority=5,
            )

    async def callback(self, query: dict, *, acknowledge=True):
        answer = "验证已失效，请联系管理员重新出题。"
        parts = query.get("data", "").split(":")
        if len(parts) == 4 and parts[0] == "v":
            _, identifier, nonce, key = parts
            async with self.db.transaction() as session:
                receipt = await session.get(Meta, f"callback:{query['id']}")
                if receipt:
                    answer = receipt.value
                else:
                    answer = await self.apply_callback(session, query, identifier, nonce, key)
                    session.add(Meta(key=f"callback:{query['id']}", value=answer))
        if acknowledge:
            await self.acknowledge(query["id"], answer)
        return answer

    async def apply_callback(self, session, query, identifier, nonce, key):
        answer = "验证已失效，请联系管理员重新出题。"
        verification = await session.get(Verification, identifier)
        if verification and verification.user_id != query["from"]["id"]:
            answer = "只有被验证者本人能够操作。"
        elif verification and verification.state == "pending" and verification.nonce == nonce:
            if query.get("message") and (
                query["message"].get("message_id") != verification.message_id
                or query["message"].get("chat", {}).get("id")
                != (verification.prompt_chat_id or verification.group_id)
            ):
                return answer
            group = await session.get(Group, verification.group_id)
            if verification.deadline <= now():
                await self.fail_verification(session, verification, "验证超时")
                answer = "验证已超时。"
            elif key == "ok":
                if len(verification.input_digits) != 6:
                    answer = "请先输入完整的 6 位数字。"
                elif secrets.compare_digest(
                    captcha.digest(verification.input_digits, nonce), verification.answer_hash
                ):
                    verification.state = "releasing"
                    verification.nonce = secrets.token_hex(4)
                    await enqueue(
                        session,
                        "release",
                        f"release:{verification.id}",
                        group.id,
                        verification.user_id,
                        {"verification_id": verification.id},
                        priority=0,
                    )
                    answer = (
                        "答案正确，正在批准入群。"
                        if verification.flow == "join_request"
                        else "答案正确，正在恢复发言。"
                    )
                else:
                    verification.attempts += 1
                    verification.input_digits = ""
                    verification.nonce = secrets.token_hex(4)
                    if verification.attempts >= group.settings["max_attempts"]:
                        await self.fail_verification(session, verification, "错误次数已用尽")
                        answer = (
                            "错误次数已用尽，正在拒绝入群申请。"
                            if verification.flow == "join_request"
                            else "错误次数已用尽，正在永久禁言，请联系管理员放行。"
                            if group.settings.get("verification_failure_action", "permanent_mute")
                            == "permanent_mute"
                            else "错误次数已用尽，正在踢出群聊，可重新入群验证。"
                        )
                    else:
                        verification.state = "muting"
                        await enqueue(
                            session,
                            "challenge",
                            f"challenge:{verification.id}:{verification.nonce}",
                            group.id,
                            verification.user_id,
                            {"verification_id": verification.id},
                            1,
                        )
                        answer = "答案不正确，正在更换图片；截止时间保持不变。"
            elif (key in "0123456789" and len(key) == 1) or key == "back":
                before = verification.input_digits
                verification.input_digits = (
                    verification.input_digits[:-1] if key == "back" else (verification.input_digits + key)[:6]
                )
                verification.updated_at = now()
                if before != verification.input_digits:
                    await self.progress.queue(session, verification)
                answer = f"已输入 {len(verification.input_digits)} / 6 位"
        return answer

    async def acknowledge(self, identifier, answer):
        try:
            await self.tg.call(
                "answer_callback_query", callback_query_id=identifier, text=answer, request_timeout=3
            )
        except Exception as exc:
            # The durable answer has already been applied; a lost toast must not replay a keypress.
            failure(logger, self.settings, exc, "验证提示回执失败（输入已保存）", f"callback={identifier}")

    async def fail_verification(self, session, verification, reason):
        if verification.state not in ("muting", "pending"):
            return
        verification.state = "failing"
        verification.error = reason
        verification.nonce = secrets.token_hex(4)
        await enqueue(
            session,
            "verification_failure",
            f"verification-failure:{verification.id}",
            verification.group_id,
            verification.user_id,
            {"verification_id": verification.id},
            priority=0,
            reason=reason,
        )

    async def verification_mute(self, session, action, group, verification, member):
        if member.restriction_conflict:
            raise ValueError("管理员已经修改成员权限，需要人工处理")
        current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id)
        if not is_present(current) or is_admin(current):
            raise ValueError("成员已经离群或成为管理员，需要人工核实")
        owned = member.owned_restriction
        if (
            owned
            and current.get("status") == "restricted"
            and (
                (
                    permission_snapshot(current) != owned.get("permissions")
                    or abs(current.get("until_date", 0) - owned.get("until_date", 0)) > 2
                )
                and (
                    permission_snapshot(current)
                    != {k: verification.previous_permissions.get(k, False) for k in ALL_PERMISSIONS}
                    or abs(
                        current.get("until_date", 0)
                        - verification.previous_permissions.get("__until_date", 0)
                    )
                    > 2
                )
            )
        ):
            raise ValueError("成员已有其他限制，需要人工处理")
        chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
        if not owned:
            previous = permission_snapshot(
                current if current["status"] == "restricted" else chat.get("permissions", {})
            )
            verification.previous_permissions = {**previous, "__until_date": current.get("until_date", 0)}
            owned = {"permissions": muted_permissions(previous), "until_date": 0}
        member.owned_restriction = {**owned, "kind": "verification", "verification_id": verification.id}
        await self.action_call(session, action,
            "restrict_chat_member",
            chat_id=group.id,
            user_id=member.user_id,
            permissions=owned["permissions"],
            use_independent_chat_permissions=True,
            until_date=0,
        )

    async def scan_timeouts(self):
        async with self.db.transaction() as session:
            for verification in (
                await session.scalars(
                    select(Verification).join(Group, Group.id == Verification.group_id).where(
                        Group.approved.is_(True),
                        Verification.state.in_(("pending", "muting")), Verification.deadline <= now()
                    )
                )
            ).all():
                await self.fail_verification(session, verification, "验证超时")

    async def message(self, message: dict, edited=False, update_id=0, archived_at_receipt=False):
        # Commit before exemptions, moderation lookups or deletion can fail.
        # Runtime already made the recording decision at receipt, even when it was off.
        if not archived_at_receipt:
            async with self.db.transaction() as session:
                await capture(session, message, edited, update_id, settings=self.settings)
        if message["chat"]["type"] not in ("supergroup", "group"):
            return
        if message.get("new_chat_members") or message.get("left_chat_member"):
            return
        group_id = message["chat"]["id"]
        sender_chat = message.get("sender_chat")
        sender = sender_chat or message.get("from")
        if not sender or sender.get("id") == self.tg.user_id:
            return
        async with self.db.transaction() as session:
            needs_admin_check = await self.moderate_message(session, message, edited, update_id)
        if not needs_admin_check:
            return
        # Check a potential violation outside the database writer. The second
        # pass reloads current configuration, exemptions and verification state;
        # an existing message fingerprint never counts as another message.
        if is_admin(await self.tg.call("get_chat_member", chat_id=group_id, user_id=sender["id"])):
            async with self.db.transaction() as session:
                await self.exempt_administrator(session, group_id, sender["id"])
            return
        async with self.db.transaction() as session:
            await self.moderate_message(session, message, edited, update_id, admin_checked=True)

    async def moderate_message(self, session, message, edited, update_id, *, admin_checked=False):
        group_id = message["chat"]["id"]
        sender_chat = message.get("sender_chat")
        sender = sender_chat or message["from"]
        group = await session.get(Group, group_id)
        if not group or not group.approved or not group.enabled:
            return
        if sender_chat and (sender_chat["id"] == group_id or message.get("is_automatic_forward")):
            return
        user_id = sender["id"]
        member = await session.get(Member, (group_id, user_id))
        if not sender_chat:
            white = await session.get(Whitelist, (group_id, user_id))
            if user_id in self.settings.superadmin_ids:
                return
            if white and white.moderation:
                # A moderation-only exemption must preserve the separate
                # verification setting. A manual unmute enables both flags.
                if not white.verification and await session.scalar(
                    select(Verification.id).where(
                        Verification.group_id == group_id,
                        Verification.user_id == user_id,
                        Verification.state.in_(ACTIVE),
                    )
                ):
                    if not admin_checked:
                        return True
                    await enqueue(
                        session,
                        "delete",
                        f"pending-message:{group_id}:{message['message_id']}",
                        group_id,
                        user_id,
                        {"message_id": message["message_id"]},
                        priority=0,
                    )
                return
            text, urls = message_content(message)
            display_name = " ".join(str(sender.get(key, "")) for key in ("first_name", "last_name")).strip()
            if sender.get("username"):
                display_name += " @" + sender["username"]
            rules = (await session.scalars(select(Rule).where(Rule.group_id == group_id))).all()
            profile = assess(
                text, urls, [row_dict(rule) for rule in rules], group.settings, sender_name=display_name
            )
            if profile["name_reasons"]:
                if not admin_checked:
                    return True
                await self.record_profile_message(
                    session, group, member, message, edited, update_id, display_name, profile
                )
                return
            if group.settings.get("emoji_status_blocked_packs") and sender.get("is_premium") and not admin_checked:
                return True
            await self.emoji_status.queue(session, group, sender, message["message_id"])
            verification = await session.scalar(
                select(Verification).where(
                    Verification.group_id == group_id,
                    Verification.user_id == user_id,
                    Verification.state.in_(ACTIVE),
                )
            )
            if verification:
                if not admin_checked:
                    return True
                await enqueue(
                    session,
                    "delete",
                    f"pending-message:{group_id}:{message['message_id']}",
                    group_id,
                    user_id,
                    {"message_id": message["message_id"]},
                    priority=0,
                )
                return
            minimum = group.settings.get("media_min_text_messages", 3)
            if minimum and visual_media(message):
                count = await text_count_before(session, message, minimum)
                if count < minimum:
                    if not admin_checked:
                        return True
                    await self.delete_early_media(session, group, message, edited, update_id, count, minimum)
                    return
        text, urls = message_content(message)
        generation = member.generation if member else ""
        existing = await session.get(Fingerprint, (group_id, user_id, message["message_id"]))
        batch_key = (
            "album:" + str(message["media_group_id"])
            if message.get("media_group_id")
            else f"m:{message['message_id']}"
        )
        if existing is None:
            album = await session.scalar(
                select(Fingerprint).where(
                    Fingerprint.group_id == group_id,
                    Fingerprint.user_id == user_id,
                    Fingerprint.generation == generation,
                    Fingerprint.batch_key == batch_key,
                )
            )
            if member and (member.passed_at or member.joined_at) and album is None:
                member.observed_messages += 1
            existing = Fingerprint(
                group_id=group_id,
                user_id=user_id,
                message_id=message["message_id"],
                generation=generation,
                sequence=album.sequence if album else member.observed_messages if member else 0,
                batch_key=batch_key,
                created_at=message["date"],
            )
            session.add(existing)
        existing.digest = content_digest(text) if text else ""
        existing.similarity_hash, existing.similarity_length = similarity_signature(text)
        await session.flush()
        window = max(
            group.settings["repeat_window"],
            group.settings.get("similar_window", 60),
            group.settings.get("burst_window", 10),
        )
        recent = (
            await session.scalars(
                select(Fingerprint)
                .where(
                    Fingerprint.group_id == group_id,
                    Fingerprint.user_id == user_id,
                    Fingerprint.generation == generation,
                    Fingerprint.created_at >= now() - window,
                )
                .order_by(Fingerprint.created_at.desc())
                .limit(500)
            )
        ).all()
        repeats = sum(
            bool(existing.digest)
            and row.digest == existing.digest
            and row.created_at >= now() - group.settings["repeat_window"]
            for row in recent
        )
        similar = sum(
            row.similarity_length >= group.settings.get("similar_min_length", 12)
            and existing.similarity_length >= group.settings.get("similar_min_length", 12)
            and row.created_at >= now() - group.settings.get("similar_window", 60)
            and is_similar(
                existing.similarity_hash,
                existing.similarity_length,
                row.similarity_hash,
                row.similarity_length,
                group.settings.get("similar_threshold", 85),
            )
            for row in recent
        )
        burst = len(
            {
                row.batch_key
                for row in recent
                if row.created_at >= now() - group.settings.get("burst_window", 10)
            }
        )
        rules = (await session.scalars(select(Rule).where(Rule.group_id == group_id))).all()
        newcomer = bool(
            member
            and (member.passed_at or member.joined_at)
            and (
                now() - (member.passed_at or member.joined_at) < group.settings["newcomer_seconds"]
                or 0 < existing.sequence <= group.settings.get("newcomer_messages", 3)
            )
        )
        display_name = " ".join(
            str(sender.get(key, "")) for key in ("first_name", "last_name", "title")
        ).strip()[:500]
        if sender.get("username"):
            display_name = (display_name + " @" + sender["username"])[:500]
        result = assess(
            text,
            urls,
            [row_dict(r) for r in rules],
            group.settings,
            newcomer,
            bool(message.get("contact")),
            repeats,
            similar,
            burst,
            display_name,
        )
        reasons = list(dict.fromkeys(result["reasons"] + result["name_reasons"]))
        if sender_chat and result["name_reasons"]:
            result["reasons"] += ["频道身份昵称命中，删除该消息"]
            reasons = list(dict.fromkeys(result["reasons"] + result["name_reasons"]))
        if not reasons:
            if not sender_chat:
                await remember_text(session, message, edited)
            return
        if not sender_chat and not admin_checked:
            return True
        await mark_reasons(session, message, edited, update_id, reasons)
        violation = await session.scalar(
            select(Violation).where(
                Violation.group_id == group_id, Violation.message_id == message["message_id"]
            )
        )
        if violation:
            violation.reasons = list(dict.fromkeys(violation.reasons + reasons))
            if result["reasons"] and not violation.message_violation:
                violation.message_violation = True
                await self.apply_penalty(session, group, violation, member)
            if result["name_reasons"] and not violation.profile_violation:
                violation.profile_violation = True
                await self.apply_penalty(session, group, violation, member)
            return
        violation = Violation(
            id=uid(),
            group_id=group_id,
            user_id=user_id,
            message_id=message["message_id"],
            excerpt=text[:600],
            sender_name=display_name,
            reasons=reasons,
            sender_chat=bool(sender_chat),
            profile_violation=bool(result["name_reasons"]),
            message_violation=bool(result["reasons"]),
            warning_active=False,
        )
        session.add(violation)
        await session.flush()
        await self.apply_penalty(session, group, violation, member)

    async def record_profile_message(
        self, session, group, member, message, edited, update_id, display_name, result
    ):
        reasons = list(dict.fromkeys(result["name_reasons"] + result["reasons"]))
        await mark_reasons(session, message, edited, update_id, reasons)
        violation = await session.scalar(
            select(Violation).where(
                Violation.group_id == group.id, Violation.message_id == message["message_id"]
            )
        )
        if violation is None:
            violation = Violation(
                id=uid(),
                group_id=group.id,
                user_id=message["from"]["id"],
                message_id=message["message_id"],
                sender_name=display_name[:500],
                excerpt=(message.get("text") or message.get("caption") or "")[:600],
                reasons=reasons,
                profile_violation=True,
                message_violation=True,
                warning_active=False,
            )
            session.add(violation)
            await session.flush()
        else:
            violation.profile_violation, violation.message_violation = True, True
            violation.reasons = list(dict.fromkeys(violation.reasons + reasons))
        await self.apply_penalty(session, group, violation, member)

    async def delete_early_media(self, session, group, message, edited, update_id, count, minimum):
        reason = f"独立文字发言不足 {minimum} 条（已记录 {count} 条），删除图片或表情整条消息"
        await mark_reasons(session, message, edited, update_id, [reason])
        violation = await session.scalar(
            select(Violation).where(
                Violation.group_id == group.id,
                Violation.message_id == message["message_id"],
            )
        )
        if violation:
            violation.reasons = list(dict.fromkeys(violation.reasons + [reason]))
            violation.message_violation = True
        else:
            sender = message["from"]
            violation = Violation(
                id=uid(),
                group_id=group.id,
                user_id=sender["id"],
                message_id=message["message_id"],
                excerpt=(message.get("text") or message.get("caption") or "")[:600],
                sender_name=" ".join(sender.get(key, "") for key in ("first_name", "last_name")).strip(),
                reasons=[reason],
                sender_chat=False,
                profile_violation=False,
                message_violation=True,
                warning_active=False,
            )
            session.add(violation)
            await session.flush()
        # This gate always deletes the complete message, including its caption.
        # Do not invoke profile penalties or create warnings for this rule.
        await enqueue(
            session,
            "delete",
            f"delete:{violation.id}",
            group.id,
            message["from"]["id"],
            {"message_id": message["message_id"], "violation_id": violation.id},
            1,
        )

    async def apply_penalty(self, session, group, violation, member, actor_id=None, reason=""):
        identifiers = []
        if violation.message_violation:
            action = await enqueue(
                session,
                "delete",
                f"delete:{violation.id}",
                group.id,
                violation.user_id,
                {"message_id": violation.message_id, "violation_id": violation.id},
                1,
                actor_id,
                reason,
            )
            identifiers.append(action.id)
        if violation.profile_violation and not violation.sender_chat:
            action = await enqueue(
                session,
                "profile_mute",
                f"profile-mute:{violation.id}",
                group.id,
                violation.user_id,
                {"violation_id": violation.id, "generation": member.generation if member else ""},
                1,
                actor_id,
                reason,
            )
            identifiers.append(action.id)
        return identifiers

    async def run_one_action(self, *, include_progress=True) -> bool:
        async with self.action_lock:
            return await self._run_one_action(include_progress=include_progress)

    async def _run_one_action(self, *, include_progress=True) -> bool:
        if include_progress:
            async with self.db.sessions() as session:
                next_action = await session.scalar(
                    select(Action)
                    .where(Action.state == "pending", Action.available_at <= now())
                    .order_by(Action.priority, Action.created_at)
                    .limit(1)
                )
                if next_action and next_action.kind in ("progress", "emoji_status_check"):
                    progress_id = next_action.id
                    is_emoji_check = next_action.kind == "emoji_status_check"
                else:
                    progress_id = None
            if progress_id:
                if is_emoji_check:
                    return await self.emoji_status.run_one(progress_id)
                return await self.progress.run_one(progress_id)
        async with ActionExecution(self.db) as session:
            action = await session.scalar(
                select(Action)
                .where(
                    Action.state == "pending",
                    Action.kind.not_in(("progress", "emoji_status_check")),
                    Action.available_at <= now(),
                )
                .order_by(Action.priority, Action.created_at)
                .limit(1)
            )
            if not action:
                return False
            action.attempts += 1
            try:
                finished = await self.execute(session, action)
                if action.state == "pending" and finished is not False:
                    action.state = "succeeded"
                if action.state == "succeeded":
                    action.error = ""
            except ActionSuperseded:
                if action.state == "pending":
                    action.state = "cancelled"
                    action.error = "成员批次或管理决定已变化，已停止旧任务"
            except TelegramRetryAfter as exc:
                failure(logger, self.settings, exc, "Telegram 任务限流", f"action={action.id} kind={action.kind} group={action.group_id}")
                action.available_at = now() + exc.retry_after
                action.error = f"Telegram 限流，{exc.retry_after} 秒后重试"
            except (TelegramForbiddenError, TelegramBadRequest, ValueError) as exc:
                action.state = "failed"
                action.error = failure(logger, self.settings, exc, "任务执行失败", f"action={action.id} kind={action.kind} group={action.group_id}", terminal=True)
                group = await session.get(Group, action.group_id) if action.group_id else None
                if group and isinstance(exc, TelegramForbiddenError):
                    group.status = "error"
                await self.mark_error(session, action)
            except Exception as exc:
                action.available_at = now() + min(300, 2 ** min(action.attempts, 8))
                action.error = failure(logger, self.settings, exc, "任务执行重试", f"action={action.id} kind={action.kind} group={action.group_id} attempt={action.attempts}", terminal=action.attempts >= 8)
                if action.attempts >= 8:
                    action.state = "failed"
                await self.mark_error(session, action)
            action.updated_at = now()
            await deletion_result(session, action)
            return True

    async def action_call(self, session, action, method, **kwargs):
        execution = session.info["action_execution"]
        try:
            result = await execution.external(lambda: self.tg.call(method, **kwargs))
            if await self.automatic_exemption(session, action):
                raise ActionSuperseded(result)
            if (
                self.administrator_protected_action(action) and method == "get_chat_member"
                and kwargs.get("chat_id") == action.group_id and kwargs.get("user_id") == action.user_id
                and is_admin(result)
            ):
                await self.exempt_administrator(session, action.group_id, action.user_id)
                raise ActionSuperseded(result)
            return result
        except ActionSuperseded as exc:
            # A sent challenge can finish after an administrator replaced it.
            # Remove that exact stale prompt without writing into the new generation.
            if method == "send_photo" and isinstance(exc.result, dict) and exc.result.get("message_id"):
                await enqueue(session, "delete", f"cleanup:late:{action.id}:{exc.result['message_id']}",
                              action.group_id, action.user_id,
                              {"chat_id": kwargs["chat_id"], "message_id": exc.result["message_id"]}, 5)
            raise

    async def automatic_exemption(self, session, action):
        if action.actor_id is not None or not action.user_id or action.dedupe_key.startswith("cleanup:"):
            return False
        verification_scope = action.kind in ("verify_mute", "challenge", "progress", "verification_failure", "ban") or action.dedupe_key.startswith("pending-message:")
        if not verification_scope and action.kind not in ("profile_mute", "emoji_status_mute", "moderation_mute", "delete"):
            return False
        if action.user_id in self.settings.superadmin_ids:
            return True
        white = await session.get(Whitelist, (action.group_id, action.user_id), populate_existing=True)
        return bool(white and (white.verification if verification_scope else white.moderation))

    async def mark_error(self, session, action):
        if action.payload.get("verification_id"):
            verification = await session.get(Verification, action.payload["verification_id"])
            if verification:
                verification.error = action.error

    async def execute(self, session, action):
        group = await session.get(Group, action.group_id) if action.group_id else None
        if group and not group.approved and not action.dedupe_key.startswith("cleanup:"):
            action.state, action.error = "cancelled", "此群未获准接入"
            return
        verification = (
            await session.get(Verification, action.payload["verification_id"])
            if (action.payload.get("verification_id"))
            else None
        )
        member = await session.get(Member, (action.group_id, action.user_id)) if action.user_id else None
        if await self.automatic_exemption(session, action):
            action.state = "cancelled"
            await deletion_result(session, action)
            return
        if action.payload.get("generation") and (
            not member or member.generation != action.payload["generation"]
        ):
            action.state = "cancelled"
            return
        if verification and (
            verification.state not in ACTIVE or not member or member.generation != verification.generation
        ):
            action.state = "cancelled"
            return
        if verification and action.payload.get("expected_nonce") not in (None, verification.nonce):
            action.state = "cancelled"
            return
        if (
            verification
            and action.kind in ("verify_mute", "challenge", "progress")
            and (verification.state not in ("muting", "pending"))
        ):
            action.state = "cancelled"
            return
        if verification and (
            action.kind == "release"
            and verification.state != "releasing"
            or action.kind == "ban"
            and verification.state != "removing"
            or action.kind == "verification_failure"
            and verification.state != "failing"
        ):
            action.state = "cancelled"
            return
        if verification and action.kind in ("verify_mute", "challenge") and verification.deadline <= now():
            await self.fail_verification(session, verification, "验证超时")
            action.state = "cancelled"
            return
        if self.administrator_protected_action(action) and not (
            verification and verification.flow == "join_request" and member and not member.present
        ):
            await self.action_call(session, action, "get_chat_member", chat_id=action.group_id, user_id=action.user_id)
        if action.kind == "verify_mute":
            await self.verification_mute(session, action, group, verification, member)
            await enqueue(
                session,
                "challenge",
                f"challenge:{verification.id}:{verification.nonce}",
                group.id,
                member.user_id,
                {"verification_id": verification.id},
                1,
            )
        elif action.kind == "challenge":
            await self.cleanup(session, verification)
            code, image = captcha.make_image()
            verification.answer_hash = captcha.digest(code, verification.nonce)
            verification.input_digits = ""
            response = await self.action_call(session, action, 
                "send_photo",
                chat_id=verification.prompt_chat_id or group.id,
                photo=image,
                caption=captcha.caption(
                    verification.user_name,
                    "",
                    verification.attempts,
                    group.settings["max_attempts"],
                    verification.deadline,
                    join_request=verification.flow == "join_request",
                    group_title=group.title,
                    failure_action=group.settings.get("verification_failure_action", "permanent_mute"),
                ),
                reply_markup=captcha.keyboard(verification.id, verification.nonce),
            )
            verification.message_id = response["message_id"]
            verification.state = "pending"
            verification.error = ""
        elif action.kind == "release" and verification and verification.flow == "join_request":
            current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id)
            if not is_present(current):
                await self.action_call(session, action, "approve_chat_join_request", chat_id=group.id, user_id=action.user_id)
            verification.state, verification.error, verification.updated_at = "passed", "", now()
            member.passed_at, member.request_until = now(), 0
            if is_present(current):
                verification.previous_permissions = {
                    **verification.previous_permissions,
                    "entry_consumed": True,
                }
                member.present = True
            await self.cleanup(session, verification)
        elif (
            action.kind in ("ban", "verification_failure")
            and verification
            and verification.flow == "join_request"
        ):
            await self.action_call(session, action, "decline_chat_join_request", chat_id=group.id, user_id=action.user_id)
            if not member.request_until or member.request_until <= now():
                member.request_until = now() + group.settings["failure_ban_seconds"]
            verification.state, verification.updated_at = "failed", now()
            verification.failure_action = "decline"
            await self.cleanup(session, verification)
        elif action.kind == "verification_failure":
            strategy = action.payload.get("applied_strategy") or group.settings.get(
                "verification_failure_action", "permanent_mute"
            )
            action.payload = {**action.payload, "applied_strategy": strategy}
            if strategy == "permanent_mute":
                await self.verification_mute(session, action, group, verification, member)
                member.owned_restriction = {
                    **member.owned_restriction,
                    "kind": "verification_failed",
                    "previous": verification.previous_permissions,
                }
            else:
                current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id)
                if is_admin(current) or member.restriction_conflict:
                    raise ValueError("成员身份或权限已变化，需要人工处理")
                if is_present(current):
                    if action.payload.get("kick_until") and (
                        permission_snapshot(current) != action.payload.get("before_kick_permissions")
                        or current.get("status") != action.payload.get("before_kick_status")
                        or current.get("until_date", 0) != action.payload.get("before_kick_until", 0)
                    ):
                        raise ValueError("移出重试前成员状态已变化，不重复踢出")
                    until = int(now() + 600)
                    action.payload = {
                        **action.payload,
                        "kick_until": until,
                        "before_kick_permissions": permission_snapshot(current),
                        "before_kick_status": current.get("status"),
                        "before_kick_until": current.get("until_date", 0),
                    }
                    member.owned_restriction = {"kind": "verification_kick", "until_date": until}
                    await self.action_call(session, action, 
                        "ban_chat_member", chat_id=group.id, user_id=member.user_id, until_date=until
                    )
                elif current.get("status") == "kicked":
                    if (
                        not action.payload.get("kick_until")
                        or abs(current.get("until_date", 0) - action.payload["kick_until"]) > 2
                    ):
                        raise ValueError("成员存在其他封禁，不自动解除")
                # A removal uses ban+unban because Telegram has no kick-only method.
                # Keep this action pending on a lost unban response; a retry only
                # clears our matching temporary ban, never a new member's restrictions.
                if current.get("status") != "left":
                    await self.action_call(session, action, 
                        "unban_chat_member", chat_id=group.id, user_id=member.user_id, only_if_banned=True
                    )
                member.owned_restriction, member.present = None, False
            verification.state, verification.failure_action, verification.updated_at = (
                "failed",
                strategy,
                now(),
            )
            verification.error = action.reason
            await self.cleanup(session, verification)
        elif action.kind in ("release", "unmute"):
            if (
                action.kind == "unmute"
                and member
                and member.owned_restriction
                and member.owned_restriction.get("kind") == "verification"
            ):
                raise ValueError("成员正在验证，请使用人工放行完成验证")
            if not member or not member.owned_restriction:
                if verification and member:
                    current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id)
                    if current.get("status") == "member" and not member.restriction_conflict:
                        verification.state, member.passed_at = "passed", now()
                        await self.cleanup(session, verification)
                        if action.actor_id:
                            await self.trust_member(
                                session,
                                member,
                                action.actor_id,
                                action.reason or "管理员人工放行",
                                exclude_action=action.id,
                            )
                        return
                raise ValueError("没有可解除的 PurrAI 限制，请核实成员当前状态")
            current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id)
            owned = member.owned_restriction
            if member.restriction_conflict or not is_present(current) or is_admin(current):
                raise ValueError("成员权限已被其他管理员修改，需要人工处理")
            already_released = bool(
                owned.get("release_permissions") is not None
                and (
                    current.get("status") == "member"
                    or (
                        permission_snapshot(current) == owned["release_permissions"]
                        and abs(current.get("until_date", 0) - owned.get("release_until", 0)) <= 2
                    )
                )
            )
            if not already_released and (
                current.get("status") == "restricted"
                and (
                    permission_snapshot(current) != owned.get("permissions")
                    or abs(current.get("until_date", 0) - owned.get("until_date", 0)) > 2
                )
            ):
                action.state = "failed"
                action.error = "成员权限已被其他管理员修改，需要人工处理"
                if verification:
                    verification.error = action.error
                return
            chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
            defaults = chat.get("permissions", {})
            previous = verification.previous_permissions if verification else owned.get("previous", {})
            previous_until = previous.get("__until_date", 0)
            if previous_until and previous_until <= now():
                previous, previous_until = {}, 0
            restore = {
                key: bool(defaults.get(key, False) and previous.get(key, True)) for key in ALL_PERMISSIONS
            }
            if not already_released:
                member.owned_restriction = {
                    **owned,
                    "release_permissions": restore,
                    "release_until": previous_until,
                }
                await self.action_call(session, action, 
                    "restrict_chat_member",
                    chat_id=group.id,
                    user_id=member.user_id,
                    permissions=restore,
                    use_independent_chat_permissions=True,
                    until_date=previous_until,
                )
            member.owned_restriction = None
            if verification:
                verification.state = "passed"
                verification.failure_action = ""
                verification.error = ""
                verification.updated_at = now()
                member.passed_at = now()
                await self.cleanup(session, verification)
            if action.actor_id:
                await self.trust_member(
                    session,
                    member,
                    action.actor_id,
                    action.reason or "管理员手动解除禁言",
                    exclude_action=action.id,
                )
        elif action.kind in ("ban", "manual_ban"):
            current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id)
            if is_admin(current) or member and member.restriction_conflict:
                raise ValueError("成员身份或权限已变化，需要人工处理")
            owned = member.owned_restriction if member else None
            reconciled = bool(owned and owned.get("source_action") == action.id and current.get("status") == "kicked"
                              and abs(current.get("until_date", 0) - owned["until_date"]) <= 2)
            if not reconciled:
                if current.get("status") == "kicked":
                    raise ValueError("成员已有其他封禁，需要人工处理")
                until = owned["until_date"] if owned and owned.get("source_action") == action.id else int(now() + group.settings["failure_ban_seconds"])
                if owned and owned.get("source_action") == action.id and until <= now():
                    member.owned_restriction = None
                    action.payload = {**action.payload, "outcome": "restriction_expired"}
                    return
                if member:
                    member.owned_restriction = {"kind": "ban", "source_action": action.id, "until_date": until}
                await self.action_call(session, action,
                    "ban_chat_member", chat_id=group.id, user_id=action.user_id, until_date=until
                )
            if verification:
                verification.state = "failed"
                verification.failure_action = "legacy_ban"
                verification.updated_at = now()
                await self.cleanup(session, verification)
        elif action.kind == "moderation_mute":
            action.state = "cancelled"
            return
        elif action.kind == "emoji_status_mute":
            await self.mute_emoji_status(session, group, member, action)
        elif action.kind == "profile_mute":
            await self.permanent_profile_mute(session, group, member, action, "profile")
        elif action.kind == "manual_mute":
            current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id)
            if not is_present(current) or is_admin(current):
                action.state = "cancelled"
                return
            if member and member.owned_restriction and member.owned_restriction.get("kind") == "verification":
                action.state = "cancelled"
                return
            if member and (
                member.restriction_conflict
                or action.payload.get("generation")
                and member.generation != action.payload["generation"]
            ):
                raise ValueError("成员权限或入群批次已变化，需要人工处理")
            if current.get("status") == "restricted" and not (member and member.owned_restriction):
                raise ValueError("保留管理员已有禁言，需要人工处理")
            if (
                current.get("status") == "restricted"
                and member
                and member.owned_restriction
                and (
                    permission_snapshot(current) != member.owned_restriction.get("permissions")
                    or abs(current.get("until_date", 0) - member.owned_restriction.get("until_date", 0)) > 2
                )
            ):
                raise ValueError("成员权限已经改变，需要人工处理")
            if member is None:
                member = Member(group_id=group.id, user_id=action.user_id, joined_at=now())
                session.add(member)
            owned = member.owned_restriction or {}
            reconciled = bool(owned.get("source_action") == action.id and current.get("status") == "restricted"
                              and permission_snapshot(current) == owned.get("permissions")
                              and abs(current.get("until_date", 0) - owned.get("until_date", 0)) <= 2)
            if owned.get("source_action") == action.id and owned["until_date"] <= now() and current.get("status") == "member":
                member.owned_restriction = None
                action.payload = {**action.payload, "outcome": "restriction_expired"}
                return
            if not reconciled:
                chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
                until = owned.get("until_date") if owned.get("source_action") == action.id else int(now() + group.settings["mute_seconds"])
                target = owned.get("permissions") if owned.get("source_action") == action.id else muted_permissions(chat.get("permissions", {}))
                member.owned_restriction = {
                    "kind": "moderation", "source_action": action.id,
                    "permissions": target, "until_date": until, "previous": {},
                }
                await self.action_call(session, action,
                    "restrict_chat_member", chat_id=group.id, user_id=action.user_id,
                    permissions=target, use_independent_chat_permissions=True, until_date=until,
                )
            await enqueue(
                session,
                "warn",
                f"mute-notice:{action.id}",
                group.id,
                action.user_id,
                {
                    "text": f"用户 {action.user_id}：{'管理员处置' if action.kind == 'manual_mute' else '累计违规'}，已临时禁言 {group.settings['mute_seconds'] // 60} 分钟。"
                },
            )
        elif action.kind == "unban" and action.payload.get("request_cooldown"):
            if member:
                member.request_until = 0
        elif action.kind == "unban":
            if not member or not member.owned_restriction or member.owned_restriction.get("kind") != "ban":
                raise ValueError("没有 PurrAI 施加的封禁记录")
            current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id)
            if member.owned_restriction.get("unban_action") == action.id and current.get("status") == "left" and not member.restriction_conflict:
                member.owned_restriction = None
                return
            if (
                member.restriction_conflict
                or current.get("status") != "kicked"
                or abs(current.get("until_date", 0) - member.owned_restriction["until_date"]) > 2
            ):
                raise ValueError("封禁状态已改变，需要人工处理")
            member.owned_restriction = {**member.owned_restriction, "unban_action": action.id}
            await self.action_call(session, action,
                "unban_chat_member", chat_id=group.id, user_id=action.user_id, only_if_banned=True
            )
            member.owned_restriction = None
        elif action.kind == "delete":
            try:
                await self.action_call(session, action, 
                    "delete_message",
                    chat_id=action.payload.get("chat_id", action.group_id),
                    message_id=action.payload["message_id"],
                )
            except TelegramBadRequest as exc:
                if "message to delete not found" not in str(exc).lower():
                    raise
                action.payload = {**action.payload, "already_absent": True}
        elif action.kind == "admin_notify":
            sources = [await session.get(Action, identifier) for identifier in action.payload["action_ids"]]
            if any(item and item.state == "pending" for item in sources):
                action.available_at = now() + 2
                return False
            names = {
                "verification_failure": "验证失败处理",
                "archive_export": "导出归档",
                "release": "放行",
                "challenge": "重新出题",
                "verify_mute": "重新禁言验证",
                "ban": "封禁",
                "manual_ban": "封禁",
                "manual_mute": "禁言",
                "unmute": "解除禁言",
                "unban": "解除封禁",
                "revoke_warning": "撤销警告",
            }
            states = {"succeeded": "已完成", "failed": "失败", "cancelled": "已取消"}
            lines = [f"PurrAI · {group.title if group else '管理'} · 执行结果"]
            for source in sources[:20]:
                if source:
                    lines.append(
                        f"{names.get(source.kind, source.kind)} · 用户 {source.user_id}："
                        f"{states.get(source.state, source.state)}"
                        + (f"\n原因：{source.error}" if source.error else "")
                    )
                    if source.kind == "join_link" and source.state == "succeeded":
                        lines.append(source.payload.get("invite_link", ""))
            if len(sources) > 20:
                lines.append(f"共 {len(sources)} 项；其余结果可在机器人操作记录查看。")
            await self.action_call(session, action, 
                "send_message", chat_id=action.payload["chat_id"], text="\n".join(lines)[:4000]
            )
        elif action.kind == "join_link":
            response = await self.action_call(session, action, 
                "create_chat_invite_link", chat_id=group.id, name="PurrAI 验证入群", creates_join_request=True
            )
            action.payload = {**action.payload, "invite_link": response["invite_link"]}
        elif action.kind == "archive_export":
            from purrai.archive import export_document
            from purrai.management import Management, ManagementError
            from purrai.schemas import ArchiveFilter

            management = Management(self.db, self.tg, self, self.settings)
            try:
                await session.info["action_execution"].external(lambda: management.authorize(action.actor_id, group.id))
            except ManagementError as exc:
                raise ValueError(str(exc)) from exc
            filters = ArchiveFilter.model_validate(action.payload["filters"])
            content, count = await session.info["action_execution"].external(
                lambda: export_document(self.db, group.id, filters, action.payload["cutoff"])
            )
            await self.action_call(session, action, 
                "send_document",
                chat_id=action.actor_id,
                document=bytes(content),
                filename=f"purrai-{group.id}-messages.jsonl",
                caption=f"{group.title} · {count} 条归档消息，包含编辑版本与处置结果。媒体仅保存标识和元数据。",
            )
            action.payload = {**action.payload, "exported_count": count}
        elif action.kind == "warn":
            await self.action_call(session, action, "send_message", chat_id=group.id, text=action.payload["text"])
        else:
            raise ValueError(f"未知任务类型：{action.kind}")

    async def mute_emoji_status(self, session, group, member, action):
        if not await self.emoji_status.eligible(session, action):
            action.state = "cancelled"
            return
        owned = member.owned_restriction if member else None
        if owned and owned.get("source_action") == action.id:
            current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id)
            if (
                current.get("status") == "restricted"
                and current.get("until_date", 0) == 0
                and permission_snapshot(current) == owned.get("permissions")
            ):
                # A lost response must not leave verification stuck if the user
                # changes their status after Telegram already applied our mute.
                await self.permanent_profile_mute(
                    session, group, member, action, "emoji_status", current=current
                )
                return
        result = await self.emoji_status.resolve(action.user_id, call=lambda method, **kw: self.action_call(session, action, method, **kw))
        if result["pack"] not in group.settings["emoji_status_blocked_packs"]:
            action.state = "cancelled"
            return
        action.payload = {**action.payload, **result}
        await self.permanent_profile_mute(session, group, member, action, "emoji_status")

    async def permanent_profile_mute(self, session, group, member, action, kind, *, current=None):
        if current is None:
            current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id)
        if not is_present(current) or is_admin(current):
            action.state = "cancelled"
            return
        if member is None:
            member = Member(group_id=group.id, user_id=action.user_id, joined_at=0)
            session.add(member)
            await session.flush()
        owned = member.owned_restriction
        already_muted = bool(
            owned
            and owned.get("kind") in ("profile", "emoji_status")
            and owned.get("until_date") == 0
            and current.get("status") == "restricted"
            and permission_snapshot(current) == owned.get("permissions")
            and current.get("until_date", 0) == 0
        )
        if member.restriction_conflict or current.get("status") == "restricted" and not owned:
            raise ValueError("成员存在其他管理员限制，保留当前限制")
        if (
            owned
            and current.get("status") == "restricted"
            and (
                permission_snapshot(current) != owned.get("permissions")
                or abs(current.get("until_date", 0) - owned.get("until_date", 0)) > 2
            )
        ):
            raise ValueError("成员限制已被其他管理员修改，保留当前限制")
        active = (
            await session.scalars(
                select(Verification).where(
                    Verification.group_id == group.id,
                    Verification.user_id == member.user_id,
                    Verification.generation == member.generation,
                    Verification.state.in_(ACTIVE),
                )
            )
        ).all()
        chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
        previous = (owned or {}).get("previous", {})
        if owned and owned.get("kind") == "verification" and active:
            previous = active[0].previous_permissions
        elif not owned:
            previous = {**permission_snapshot(chat.get("permissions", {})), "__until_date": 0}
        target = muted_permissions(previous or chat.get("permissions", {}))
        member.owned_restriction = {
            "kind": kind,
            "source_action": action.id,
            "permissions": target,
            "until_date": 0,
            "previous": previous,
            "pack": action.payload.get("pack", ""),
            **({"verification_id": active[0].id} if active else {}),
        }
        for verification in active:
            verification.state = "failing"
            verification.nonce = secrets.token_hex(4)
            verification.error = "昵称或状态表情违规；等待永久禁言完成"
        if not already_muted:
            await self.action_call(session, action, 
                "restrict_chat_member",
                chat_id=group.id,
                user_id=member.user_id,
                permissions=target,
                use_independent_chat_permissions=True,
                until_date=0,
            )
        # Report completion only after Telegram accepted the permanent restriction.
        for verification in active:
            verification.state, verification.failure_action, verification.updated_at = (
                "failed",
                "permanent_mute",
                now(),
            )
            verification.error = "昵称或状态表情违规，已永久禁言"
            await self.cleanup(session, verification)
        if kind != "emoji_status":
            return
        violation = Violation(
            id=uid(),
            group_id=group.id,
            user_id=member.user_id,
            message_id=-int(action.id[:15], 16),
            excerpt="名字旁状态表情检查",
            reasons=[f"状态表情包黑名单：{action.payload['pack']}；永久禁言"],
            profile_violation=True,
            message_violation=False,
            warning_active=False,
        )
        session.add(violation)
        await session.flush()
        action.payload = {**action.payload, "violation_id": violation.id}
        for message_id in action.payload.get("message_ids", []):
            existing = await session.scalar(
                select(Action.id)
                .where(
                    Action.group_id == group.id,
                    Action.kind == "delete",
                    Action.state != "cancelled",
                    func.coalesce(Action.payload["chat_id"].as_integer(), Action.group_id) == group.id,
                    Action.payload["message_id"].as_integer() == message_id,
                )
                .limit(1)
            )
            if existing:
                continue
            await enqueue(
                session,
                "delete",
                f"emoji-message:{group.id}:{message_id}",
                group.id,
                member.user_id,
                {"message_id": message_id, "violation_id": violation.id},
                0,
            )

    async def trust_member(self, session, member, actor_id, reason, exclude_action=None):
        white = await session.get(Whitelist, (member.group_id, member.user_id))
        if white is None:
            white = Whitelist(group_id=member.group_id, user_id=member.user_id)
            session.add(white)
        white.verification, white.moderation = True, True
        white.note = f"管理员 {actor_id} 手动解禁后自动加入白名单：{reason}"[:300]
        for pending in await session.scalars(
            select(Action).where(
                Action.group_id == member.group_id,
                Action.user_id == member.user_id,
                Action.state == "pending",
                Action.actor_id.is_(None),
                Action.kind.in_(
                    (
                        "profile_mute",
                        "emoji_status_check",
                        "emoji_status_mute",
                        "verify_mute",
                        "challenge",
                        "progress",
                        "verification_failure",
                        "ban",
                        "moderation_mute",
                        "delete",
                    )
                ),
            )
        ):
            if pending.id != exclude_action and not pending.dedupe_key.startswith("cleanup:"):
                pending.state, pending.error, pending.updated_at = (
                    "cancelled",
                    "管理员解禁，已加入本群白名单",
                    now(),
                )
                await deletion_result(session, pending)
        for verification in await session.scalars(
            select(Verification).where(
                Verification.group_id == member.group_id,
                Verification.user_id == member.user_id,
                Verification.generation == member.generation,
                Verification.state.in_(ACTIVE),
            )
        ):
            verification.state, verification.error, verification.updated_at = "passed", "", now()
            verification.nonce = secrets.token_hex(4)
            await self.cleanup(session, verification)
        member.passed_at = now()
        member.request_until = 0
        await audit(
            session,
            "whitelist",
            actor_id,
            member.group_id,
            reason,
            {"user_id": member.user_id, "verification": True, "moderation": True},
        )

    async def action_loop(self):
        while True:
            try:
                if not self.tg.connected or not self.tg.user_id:
                    await asyncio.sleep(0.3)
                    continue
                await self.scan_timeouts()
                if not await self.run_one_action(include_progress=False):
                    await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "任务循环异常", terminal=True)
                await asyncio.sleep(2)


def row_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
