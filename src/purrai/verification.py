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
    Inbox,
    Member,
    Meta,
    Verification,
    Whitelist,
    now,
    uid,
)
from purrai.verification_cleanup import cleanup, observe_callback_prompt
from purrai.verification_progress import queue_progress
from purrai.verification_state import ACTIVE
from purrai.subscription_verification import schedule_check

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
            session.add(Meta(key=key, value="received", expires_at=now() + 7 * 86400))
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
                method=group.settings.get("verification_method", "image"),
                challenge={"channel": group.settings.get("verification_channel", "")},
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

    async def callback(self, query: dict, *, acknowledge=True, received_at=None):
        answer = "验证已失效，请联系管理员重新出题。"
        parts = query.get("data", "").split(":")
        if len(parts) == 4 and parts[0] == "v":
            _, identifier, nonce, key = parts
            async with self.db.transaction() as session:
                receipt = await session.get(Meta, f"callback:{query['id']}")
                if receipt:
                    answer = receipt.value
                else:
                    answer = await self.apply_callback(session, query, identifier, nonce, key, received_at=received_at)
                    session.add(Meta(key=f"callback:{query['id']}", value=answer, expires_at=now() + 7 * 86400))
        if acknowledge:
            await self.acknowledge(query["id"], answer)
        return answer

    async def apply_callback(self, session, query, identifier, nonce, key, *, received_at=None):
        answer = "验证已失效，请联系管理员重新出题。"
        verification = await session.get(Verification, identifier)
        if verification:
            await observe_callback_prompt(session, verification, query, nonce, self.tg.user_id, received_at=received_at)
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
            receipt_time = received_at if received_at is not None else now()
            if verification.deadline <= receipt_time:
                await self.fail_verification(session, verification, "验证超时")
                answer = "验证已超时。"
            elif verification.method == "channel":
                await schedule_check(session, verification, immediate=True)
                answer = "正在核实订阅，符合条件后会自动放行。"
            elif verification.choices or key == "ok":
                if verification.choices:
                    if key not in {f"c{index}" for index in range(len(verification.choices))}:
                        return "请点击当前题目下方的完整答案按钮。"
                    verification.input_digits = verification.choices[int(key[1:])]
                    verification.updated_at = now()
                if not verification.choices and len(verification.input_digits) != 6:
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
                        await cleanup(session, verification)
                        verification.message_id = None
                        verification.challenge = {}
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
                        answer = "答案不正确，正在更换题目；新题目送达后重新计时。"
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

    @staticmethod
    async def queued_answer(session, verification):
        return await session.scalar(select(Inbox.id).where(
            Inbox.state.in_(("pending", "failed")), Inbox.received_at > 0,
            Inbox.received_at < verification.deadline,
            Inbox.payload["callback_query"]["from"]["id"].as_integer() == verification.user_id,
            Inbox.payload["callback_query"]["data"].as_string().startswith(f"v:{verification.id}:{verification.nonce}:"),
        ).limit(1)) is not None

    async def acknowledge(self, identifier, answer):
        try:
            await self.tg.call(
                "answer_callback_query", callback_query_id=identifier, text=answer, request_timeout=3
            )
        except Exception as exc:
            # The durable answer has already been applied; a lost toast must not replay a keypress.
            failure(logger, self.settings, exc, "验证提示回执失败（输入已保存）", f"callback={identifier}")

    async def fail_verification(self, session, verification, reason, *, subscription_confirmed=False):
        if verification.state not in ("muting", "pending"):
            return
        if not verification.message_id:
            return
        if verification.method == "channel" and reason == "验证超时" and not subscription_confirmed:
            await schedule_check(session, verification)
            return
        if reason == "验证超时" and await self.queued_answer(session, verification):
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
                        Verification.message_id.is_not(None),
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
