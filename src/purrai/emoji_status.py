"""Inspect the profile status, not stickers sent in messages."""

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from purrai.models import Action, Group, Member, Whitelist, now, uid
from purrai.diagnostics import failure
from purrai.telegram import is_admin

from purrai.pack_names import DEFAULT_BLOCKED_PACKS as DEFAULT_BLOCKED_PACKS

from purrai.pack_names import emoji_pack_name

pack_name = emoji_pack_name

logger = logging.getLogger(__name__)




class EmojiStatusGuard:
    def __init__(self, db, telegram, settings, enqueue, exempt_administrator):
        self.db, self.tg, self.settings, self.enqueue = db, telegram, settings, enqueue
        self.exempt_administrator = exempt_administrator
        self.lock = asyncio.Lock()

    async def queue(self, session, group, user, message_id=None):
        # Telegram supplies is_premium=True for users able to set an emoji status.
        if (
            not group.settings.get("emoji_status_blocked_packs")
            or not user.get("is_premium")
            or user.get("is_bot")
        ):
            return
        member = await session.get(Member, (group.id, user["id"]))
        if member is None:
            member = Member(group_id=group.id, user_id=user["id"], joined_at=0)
            session.add(member)
            await session.flush()
        if member.owned_restriction and member.owned_restriction.get("kind") == "emoji_status":
            return
        key = f"emoji-check:{group.id}:{user['id']}:{member.generation}"
        action = await session.scalar(select(Action).where(Action.dedupe_key == key))
        if action is None:
            await self.enqueue(
                session,
                "emoji_status_check",
                key,
                group.id,
                user["id"],
                {
                    "generation": member.generation,
                    "message_ids": [message_id] if message_id else [],
                    "cycle": uid(),
                },
                priority=1,
            )
        elif action.state != "pending":
            action.state, action.attempts, action.error, action.available_at = "pending", 0, "", now()
            action.payload = {
                "generation": member.generation,
                "message_ids": [message_id] if message_id else [],
                "cycle": uid(),
            }
        elif message_id:
            action.payload = {
                **action.payload,
                "message_ids": list(dict.fromkeys([*action.payload.get("message_ids", []), message_id])),
            }

    async def resolve(self, user_id, *, call=None):
        call = call or self.tg.call
        profile = await call("get_chat", chat_id=user_id, request_timeout=3)
        if profile.get("type") != "private" or profile.get("id") != user_id:
            raise ValueError("Telegram 未返回该用户的个人资料，无法核实表情状态")
        identifier = profile.get("emoji_status_custom_emoji_id")
        expires = profile.get("emoji_status_expiration_date", 0)
        if not identifier or expires and expires <= now():
            return {"pack": "", "custom_emoji_id": ""}
        stickers = await call("get_custom_emoji_stickers", custom_emoji_ids=[identifier], request_timeout=3)
        sticker = next((s for s in stickers if s.get("custom_emoji_id") == identifier), None)
        if sticker is None:
            raise ValueError("Telegram 未返回状态表情资料，无法核实表情包")
        return {"pack": sticker.get("set_name", "").lower(), "custom_emoji_id": identifier}

    async def eligible(self, session, action):
        group = await session.get(Group, action.group_id)
        member = await session.get(Member, (action.group_id, action.user_id))
        white = await session.get(Whitelist, (action.group_id, action.user_id))
        if (
            not group
            or not group.approved
            or not group.enabled
            or not group.settings.get("emoji_status_blocked_packs")
            or not member
            or not member.present
            or member.generation != action.payload.get("generation")
            or action.user_id in self.settings.superadmin_ids
            or white
            and white.moderation
        ):
            return None
        return group

    async def run_one(self, identifier=None):
        async with self.lock:
            async with self.db.transaction() as session:
                query = select(Action).where(
                    Action.kind == "emoji_status_check",
                    Action.state == "pending",
                    Action.available_at <= now(),
                )
                if identifier:
                    query = query.where(Action.id == identifier)
                action = await session.scalar(query.order_by(Action.created_at).limit(1))
                if not action:
                    return False
                if not await self.eligible(session, action):
                    action.state, action.updated_at = "cancelled", now()
                    return True
                identifier, user_id, group_id = action.id, action.user_id, action.group_id
                action.attempts += 1
            result, error, administrator = None, None, False
            try:
                administrator = is_admin(await self.tg.call(
                    "get_chat_member", chat_id=group_id, user_id=user_id, request_timeout=3,
                ))
                if not administrator:
                    result = await self.resolve(user_id)
            except Exception as exc:
                error = exc
            async with self.db.transaction() as session:
                action = await session.get(Action, identifier)
                if action.state != "pending":
                    return True
                group = await self.eligible(session, action)
                if not group:
                    action.state = "cancelled"
                elif administrator:
                    await self.exempt_administrator(session, group_id, user_id)
                elif isinstance(error, TelegramRetryAfter):
                    failure(
                        logger,
                        self.settings,
                        error,
                        "状态表情检查限流",
                        f"action={identifier} group={action.group_id}",
                    )
                    action.available_at = now() + error.retry_after
                    action.error = f"Telegram 限流，{error.retry_after} 秒后重查状态表情"
                elif isinstance(error, (TelegramBadRequest, TelegramForbiddenError, ValueError)):
                    action.state = "failed"
                    diagnostic = failure(
                        logger,
                        self.settings,
                        error,
                        "状态表情检查失败",
                        f"action={identifier} group={action.group_id}",
                        terminal=True,
                    )
                    action.error = f"未执行处罚；{diagnostic}"
                elif error:
                    action.available_at = now() + min(300, 2 ** min(action.attempts, 8))
                    diagnostic = failure(
                        logger,
                        self.settings,
                        error,
                        "状态表情检查重试",
                        f"action={identifier} group={action.group_id}",
                        terminal=action.attempts >= 8,
                    )
                    action.error = f"未执行处罚；{diagnostic}"
                    if action.attempts >= 8:
                        action.state = "failed"
                else:
                    action.payload = {**action.payload, **result, "checked_at": now()}
                    if result["pack"] in group.settings["emoji_status_blocked_packs"]:
                        message_ids = action.payload.get("message_ids", [])
                        penalty = await self.enqueue(
                            session,
                            "emoji_status_mute" if message_ids else "emoji_status_ban",
                            f"emoji-ban:{action.id}:{action.payload.get('cycle', '')}:{result['custom_emoji_id']}",
                            action.group_id,
                            action.user_id,
                            {
                                **result,
                                "generation": action.payload["generation"],
                                "message_ids": action.payload.get("message_ids", []),
                            },
                            priority=0,
                            reason=f"名字旁状态表情命中黑名单：{result['pack']}",
                        )
                        action.payload = {**action.payload, "penalty_action_id": penalty.id}
                    action.state, action.error = "succeeded", ""
                action.updated_at = now()
            return True

    async def loop(self):
        while True:
            try:
                if not self.tg.connected or not self.tg.user_id or not await self.run_one():
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "状态表情循环异常")
                await asyncio.sleep(1)
