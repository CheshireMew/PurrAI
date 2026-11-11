"""Ordered inbox claims, verification barriers and durable event retries."""

import asyncio
import logging

from sqlalchemy import func, or_, select

from purrai.diagnostics import failure
from purrai.models import Inbox, Verification, now
from purrai.private_access import approved, update_group_id
from purrai.work_claims import WorkClaims

logger = logging.getLogger(__name__)


class InboxProcessor:
    def __init__(self, db, telegram, settings, dispatch, current_update_id):
        self.db, self.tg, self.settings = db, telegram, settings
        self.dispatch, self.current_update_id = dispatch, current_update_id
        self.claims = WorkClaims()
        self.ready_events = {lane: asyncio.Event() for lane in ("messages", "control", "verification")}

    @staticmethod
    def inbox_scope(payload):
        callback = payload.get("callback_query", {})
        data = callback.get("data", "").split(":")
        if len(data) == 4 and data[0] == "v":
            return "verification", data[1]
        for field in ("message", "edited_message", "chat_member", "my_chat_member", "chat_join_request"):
            event = payload.get(field, {})
            chat = event.get("chat", {})
            if chat.get("type") == "private":
                return "control", chat["id"]
            if chat.get("type") in ("group", "supergroup"):
                return "group", chat["id"]
        if callback:
            return "control", callback.get("from", {}).get("id")
        return "update", payload["update_id"]

    async def claim_inbox(self, lane):
        is_verification = func.coalesce(Inbox.payload["callback_query"]["data"].as_string().like("v:%"), False)
        is_control = or_(
            func.coalesce(Inbox.payload["message"]["chat"]["type"].as_string() == "private", False),
            func.coalesce(Inbox.payload["edited_message"]["chat"]["type"].as_string() == "private", False),
            func.coalesce(Inbox.payload["callback_query"]["data"].as_string().like("a:%"), False),
        )
        query = select(Inbox).where(Inbox.state == "pending")
        if lane == "verification":
            query = query.where(is_verification)
        elif lane == "messages":
            query = query.where(~is_verification, ~is_control)
        elif lane == "control":
            query = query.where(is_control)
        async with self.claims.lock:
            async with self.db.sessions() as session:
                if lane == "verification":
                    item = await self.verification_item(session, query)
                    if item is None:
                        return None
                    scope = self.inbox_scope(item.payload)
                    if self.claims.claim(item.id, scope):
                        return item.id, item.payload, scope
                    return None
                cursor, blocked = -1, set()
                while True:
                    rows = (await session.scalars(query.where(Inbox.id > cursor).order_by(Inbox.id).limit(100))).all()
                    if not rows:
                        return None
                    for item in rows:
                        cursor = item.id
                        scope = self.inbox_scope(item.payload)
                        if scope in blocked:
                            continue
                        # A delayed or in-flight update owns only its group's or
                        # administrator's stream. Other streams remain runnable.
                        blocked.add(scope)
                        if item.available_at > now() or not self.claims.available(item.id, scope):
                            continue
                        if scope[0] == "verification" and await self.verification_barrier(session, item):
                            continue
                        self.claims.claim(item.id, scope)
                        return item.id, item.payload, scope

    async def process_one(self, lane=None):
        claimed = await self.claim_inbox(lane)
        if claimed is None:
            return False
        identifier, payload, scope = claimed
        token = self.current_update_id.set(identifier)
        try:
            async with self.db.sessions() as session:
                group_id = await update_group_id(session, payload)
                permitted = group_id is None or await approved(session, group_id)
            if not permitted:
                async with self.db.transaction() as session:
                    item = await session.get(Inbox, identifier)
                    item.state, item.payload, item.error = "done", {"update_id": identifier}, "此群未获准接入，已跳过"
                    item.completed_at = now()
                return True
            await self.dispatch(payload)
            async with self.db.transaction() as session:
                item = await session.get(Inbox, identifier)
                item.state = "done"
                item.completed_at = now()
                # The separate archive is durable; inbox is only the retry input.
                item.payload = {"update_id": identifier}
                item.error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.db.transaction() as session:
                item = await session.get(Inbox, identifier)
                item.attempts += 1
                item.available_at = now() + min(300, 2 ** min(item.attempts, 8))
                item.error = failure(logger, self.settings, exc, "事件处理失败", f"update={identifier} lane={lane}", terminal=item.attempts >= 8)
                if item.attempts >= 8:
                    item.state = "failed"
        finally:
            self.current_update_id.reset(token)
            self.claims.release(identifier, scope)
            for event in self.ready_events.values():
                event.set()
        return True

    async def verification_barrier(self, session, item):
        data = item.payload.get("callback_query", {}).get("data", "").split(":")
        verification = await session.get(Verification, data[1]) if len(data) == 4 else None
        if verification is None:
            return False
        relevant = []
        for key in ("chat_member", "my_chat_member", "chat_join_request"):
            event = Inbox.payload[key]
            same_group = event["chat"]["id"].as_integer() == verification.group_id
            user = event["from"]["id"] if key == "chat_join_request" else event["new_chat_member"]["user"]["id"]
            relevant.append(same_group if key == "my_chat_member" else same_group & (user.as_integer() == verification.user_id))
        return await session.scalar(select(Inbox.id).where(
            Inbox.id < item.id, Inbox.state != "done", or_(*relevant),
        ).limit(1)) is not None

    async def verification_item(self, session, query):
        # The oldest key owns its verification stream, not every group's input.
        # Page through all candidates so a blocked stream cannot hide another one.
        cursor, blocked = -1, set()
        while True:
            batch = (await session.scalars(query.where(Inbox.id > cursor).order_by(Inbox.id).limit(100))).all()
            if not batch:
                return None
            for item in batch:
                cursor = item.id
                data = item.payload.get("callback_query", {}).get("data", "").split(":")
                stream = data[1] if len(data) == 4 else str(item.id)
                if stream in blocked:
                    continue
                blocked.add(stream)
                if item.available_at > now():
                    continue
                if not self.claims.available(item.id, self.inbox_scope(item.payload)):
                    continue
                if await self.verification_barrier(session, item):
                    continue
                return item

    async def inbox_loop(self, lane="messages"):
        while True:
            try:
                if not self.tg.connected or not self.tg.user_id:
                    await asyncio.sleep(0.2)
                    continue
                event = self.ready_events[lane]
                event.clear()
                if not await self.process_one(lane):
                    try:
                        await asyncio.wait_for(event.wait(), timeout=0.2)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "Telegram 收件循环异常", f"lane={lane}", terminal=True)
                await asyncio.sleep(2)
