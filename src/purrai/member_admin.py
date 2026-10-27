"""Administration operations for member admin."""

import secrets

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.errors import ManagementError
from purrai.models import (
    Action,
    Group,
    Member,
    Verification,
    Violation,
    now,
)
from purrai.record_operations import retryable_verification
from purrai.restriction_state import owns_verification_ban, restriction_identity
from purrai.schemas import (
    ActionInput,
)
from purrai.serialization import row_dict
from purrai.verification_state import ACTIVE


class MemberAdministration:
    def __init__(self, context):
        self.context = context

    async def verification_action(self, actor, identifier, body: ActionInput):
        async with self.context.db.sessions() as session:
            row = await session.get(Verification, identifier)
        if not row:
            raise ManagementError(404, "验证记录不存在")
        await self.context.access.authorize(actor, row.group_id, write=True)
        async with self.context.access.group_transaction(row.group_id) as session:
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
                retryable_verification(row.state, row.failure_action)
                and body.action in ("release", "reissue")
            )
            if row.state not in ACTIVE and body.action != "unban" and not retry_muted:
                raise ManagementError(409, "此验证已经结束，请刷新记录")
            if body.action not in ("release", "reissue", "ban", "unban"):
                raise ManagementError(422, "此记录不支持该操作")
            member = await session.get(Member, (row.group_id, row.user_id))
            if not member or member.generation != row.generation:
                raise ManagementError(409, "该成员已重新入群或入群批次已变化，请刷新记录")
            if body.action == "unban" and (
                row.state != "failed"
                or (row.flow == "join_request" and member.request_until <= now())
                or (row.flow != "join_request" and not await owns_verification_ban(session, member, row))
            ):
                raise ManagementError(409, "该验证对应的封禁已结束或被替换，请从成员操作中核实当前限制")
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
                row.attempts, row.failure_action, row.error = 0, "", ""
                row.updated_at = now()
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
                else {
                    "source_verification_id": identifier,
                    "request_cooldown": row.flow == "join_request",
                    "generation": member.generation,
                    "restriction": restriction_identity(member),
                },
                0,
                actor,
                body.reason,
            )
            return row_dict(action)

    async def release_pending(self, actor, group_id, body: ActionInput):
        await self.context.access.authorize(actor, group_id, write=True)
        if body.action != "release":
            raise ManagementError(422, "批量操作只支持放行")
        async with self.context.access.group_transaction(group_id) as session:
            key = f"batch:{actor}:{body.request_id}"
            existing = await session.scalar(select(Action).where(Action.dedupe_key == key))
            if existing:
                if existing.group_id != group_id:
                    raise ManagementError(409, "请求标识已用于其他群")
                return {"action_ids": existing.payload["action_ids"], "batch_id": existing.id}
            batch = await enqueue(
                session,
                "batch_release",
                key,
                group_id,
                actor_id=actor,
                reason=body.reason,
                payload={"action_ids": []},
            )
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
                    batch_id=batch.id,
                )
                identifiers.append(action.id)
            batch.payload = {"action_ids": identifiers}
            batch.state = "succeeded"
            return {"action_ids": identifiers, "batch_id": batch.id}

    async def violation_action(self, actor, group_id, identifier, body: ActionInput):
        await self.context.access.authorize(actor, group_id, write=True)
        async with self.context.access.group_transaction(group_id) as session:
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
            payload = {"violation_id": row.id}
            if body.action in ("unmute", "unban"):
                member = await session.get(Member, (group_id, row.user_id))
                owned = member.owned_restriction if member else None
                source = (
                    await session.get(Action, owned["source_action"])
                    if owned and owned.get("source_action")
                    else None
                )
                if not source or source.payload.get("violation_id") != identifier:
                    raise ManagementError(409, "该违规对应的限制已结束或被替换，请从成员操作中核实当前限制")
                payload.update(generation=member.generation, restriction=restriction_identity(member))
            action = await enqueue(
                session,
                body.action,
                f"correction:{actor}:{body.request_id}",
                group_id,
                row.user_id,
                payload,
                0,
                actor,
                body.reason,
            )
            if body.action == "revoke_warning":
                row.warning_active, action.state = False, "succeeded"
            return row_dict(action)

    async def member_action(self, actor, group_id, user_id, action, request_id, reason="Telegram 管理员操作"):
        await self.context.access.authorize(actor, group_id, write=True)
        if user_id <= 0 or action not in ("mute", "ban", "unmute", "unban", "release", "reissue"):
            raise ManagementError(422, "请选择有效的成员和操作")
        async with self.context.db.sessions() as session:
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
        async with self.context.access.group_transaction(group_id) as session:
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
            payload = {"generation": member.generation}
            if kind in ("unmute", "unban"):
                payload["restriction"] = restriction_identity(member)
            row = await enqueue(session, kind, key, group_id, user_id, payload, 0, actor, reason)
            return row_dict(row)
