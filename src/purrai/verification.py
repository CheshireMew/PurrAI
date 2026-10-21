"""Verification input, join requests, deadlines and state transitions."""

import asyncio
import logging
import secrets

from sqlalchemy import select

from purrai import captcha
from purrai.action_queue import enqueue
from purrai.diagnostics import failure
from purrai.models import (
    Group,
    Member,
    Meta,
    Verification,
    Whitelist,
    now,
    uid,
)
from purrai.verification_cleanup import cleanup
from purrai.verification_progress import queue_progress
from purrai.verification_state import ACTIVE

logger = logging.getLogger(__name__)


class VerificationService:
    def __init__(self, db, telegram, settings):
        self.db, self.tg, self.settings = db, telegram, settings

    async def join_request(self, event, event_id=0):
        async with self.db.transaction() as session:
            group = await session.get(Group, event["chat"]["id"])
            if (
                not group
                or not group.approved
                or not group.enabled
                or not group.settings.get("join_requests")
            ):
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
                await cleanup(session, item)
            member.generation, member.present, member.passed_at = uid(), False, 0
            member.join_observed = False
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
            elif verification.choices or key == "ok":
                if verification.choices:
                    if key not in {f"c{index}" for index in range(len(verification.choices))}:
                        return "请点击当前图片下方的完整答案按钮。"
                    verification.input_digits = verification.choices[int(key[1:])]
                    verification.updated_at = now()
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
                        f"release:{verification.id}:{verification.nonce}",
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
                    await queue_progress(session, verification)
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
        await cleanup(session, verification)
        await enqueue(
            session,
            "verification_failure",
            f"verification-failure:{verification.id}:{verification.nonce}",
            verification.group_id,
            verification.user_id,
            {"verification_id": verification.id},
            priority=0,
            reason=reason,
        )

    async def scan_timeouts(self):
        async with self.db.transaction() as session:
            for verification in (
                await session.scalars(
                    select(Verification)
                    .join(Group, Group.id == Verification.group_id)
                    .where(
                        Group.approved.is_(True),
                        Verification.state.in_(("pending", "muting")),
                        Verification.deadline <= now(),
                    )
                )
            ).all():
                await self.fail_verification(session, verification, "验证超时")

    async def timeout_loop(self):
        while True:
            try:
                if self.tg.connected and self.tg.user_id:
                    await self.scan_timeouts()
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "验证超时检查异常", terminal=True)
                await asyncio.sleep(2)
