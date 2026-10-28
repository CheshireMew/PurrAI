import asyncio
import logging
from contextvars import ContextVar

from aiogram import Dispatcher
from aiogram.types import Update
from sqlalchemy import func, or_, select

from purrai.models import Inbox, Meta, Verification, now
from purrai.diagnostics import failure
from purrai.management import Management
from purrai.bot_admin import BotAdmin
from purrai.chat import GroupChat
from purrai.telegram import telegram_data
from purrai.private_access import approved, discover, update_group_id
from purrai.traffic import RateLimit
from purrai.work_claims import WorkClaims
from purrai.group_link_identity import remember_update_links

logger = logging.getLogger(__name__)
ALLOWED_UPDATES = [
    "chat_member",
    "my_chat_member",
    "chat_join_request",
    "message",
    "edited_message",
    "callback_query",
]


class Runtime:
    def __init__(self, db, telegram, service):
        self.db, self.tg, self.service = db, telegram, service
        self.chat = GroupChat(db, telegram, service.settings)
        self.management = Management(db, telegram, settings=service.settings, chat=self.chat, access=service.access)
        self.admin = BotAdmin(self.management)
        self.dispatcher = Dispatcher()
        self.tasks = []
        self.ack_tasks = set()
        self.ack_slots = asyncio.Semaphore(8)
        self.inbox_ready = {lane: asyncio.Event() for lane in ("messages", "control", "verification")}
        self.inbox_claims = WorkClaims()
        self.last_poll = 0.0
        self.last_error = ""
        self.current_update_id = ContextVar("purrai_update_id", default=0)
        self.menu_configured = False
        self.control_limit = RateLimit(per_key=120, total=600)
        self.dispatcher.chat_member.register(self.member)
        self.dispatcher.my_chat_member.register(self.bot_member)
        self.dispatcher.chat_join_request.register(self.join_request)
        self.dispatcher.callback_query.register(self.callback)
        self.dispatcher.message.register(self.message)
        self.dispatcher.edited_message.register(self.edited)

    async def member(self, event):
        await self.service.member_event(telegram_data(event), event_id=self.current_update_id.get())

    async def bot_member(self, event):
        await self.service.member_event(telegram_data(event), True)

    async def join_request(self, event):
        await self.service.join_request(telegram_data(event), self.current_update_id.get())

    async def callback(self, event):
        query = telegram_data(event)
        if not await self.admin.callback(query):
            answer = await self.service.callback(query, acknowledge=False)
            # Input is committed before acknowledgement, but slow toasts never
            # hold up the next digit. Bound both the task count and HTTP lifetime.
            if len(self.ack_tasks) < 64:
                task = asyncio.create_task(self.answer_callback(query["id"], answer))
                self.ack_tasks.add(task)
                task.add_done_callback(self.ack_tasks.discard)

    async def answer_callback(self, identifier, answer):
        try:
            async with asyncio.timeout(4):
                async with self.ack_slots:
                    await self.service.acknowledge(identifier, answer)
        except TimeoutError as exc:
            failure(logger, self.service.settings, exc, "验证提示回执超时（输入已保存）", f"callback={identifier}")

    async def message(self, event):
        message = telegram_data(event)
        # Commands still pass moderation: pending members cannot evade cleanup with /help.
        if message["chat"]["type"] != "private":
            await self.service.message(
                message, update_id=self.current_update_id.get(), archived_at_receipt=True
            )
        await self.admin.message(message)
        await self.chat.observe(message, update_id=self.current_update_id.get())

    async def edited(self, event):
        message = telegram_data(event)
        await self.service.message(
            message,
            True,
            update_id=self.current_update_id.get(),
            archived_at_receipt=True,
        )
        await self.chat.observe(message, edited=True, update_id=self.current_update_id.get())

    async def persist(self, updates: list[dict]):
        if not updates:
            return
        async with self.db.transaction() as session:
            for payload in updates:
                bot_event = payload.get("my_chat_member")
                if bot_event:
                    await discover(session, bot_event["chat"], self.service.settings.pending_group_limit)
                group_id = await update_group_id(session, payload)
                if group_id is not None and not await approved(session, group_id):
                    continue
                await remember_update_links(session, payload)
                message = payload.get("message") or payload.get("edited_message") or {}
                callback = payload.get("callback_query") or {}
                if message.get("chat", {}).get("type") == "private" or callback.get("data", "").startswith("a:"):
                    actor = (message.get("from") or callback.get("from") or {}).get("id")
                    if not self.control_limit.allow(actor):
                        continue
                if not await session.get(Inbox, payload["update_id"]):
                    from purrai.archive import capture

                    message = payload.get("message") or payload.get("edited_message")
                    if message:
                        await capture(session, message, "edited_message" in payload, payload["update_id"], settings=self.service.settings)
                    session.add(
                        Inbox(
                            id=payload["update_id"],
                            payload=payload,
                            state="pending",
                            attempts=0,
                            available_at=now(),
                        )
                    )
            offset = await session.get(Meta, "poll_offset")
            value = str(max(item["update_id"] for item in updates) + 1)
            if offset:
                offset.value = str(max(int(offset.value), int(value)))
            else:
                session.add(Meta(key="poll_offset", value=value))
        for event in self.inbox_ready.values():
            event.set()

    async def poll_loop(self):
        while True:
            try:
                if not self.tg.connected:
                    await self.tg.initialize()
                    webhook = await self.tg.call("get_webhook_info")
                    if webhook.get("url"):
                        self.last_error = "该 Bot 已配置 Webhook，请先人工确认并移除后再运行长轮询"
                        self.tg.connected = False
                        await asyncio.sleep(30)
                        continue
                if not self.menu_configured:
                    try:
                        await self.tg.register_commands()
                        self.menu_configured = True
                    except Exception as exc:
                        failure(logger, self.service.settings, exc, "Telegram 命令菜单更新失败")
                        self.menu_configured = True
                async with self.db.sessions() as session:
                    offset = await session.get(Meta, "poll_offset")
                    value = int(offset.value) if offset else None
                updates = await self.tg.call(
                    "get_updates", offset=value, timeout=25, allowed_updates=ALLOWED_UPDATES
                )
                await self.persist(telegram_data(updates))
                self.last_poll = now()
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = failure(logger, self.service.settings, exc, "Telegram 轮询失败")
                self.tg.connected = False
                await asyncio.sleep(5)

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
        async with self.inbox_claims.lock:
            async with self.db.sessions() as session:
                if lane == "verification":
                    item = await self.verification_item(session, query)
                    if item is None:
                        return None
                    scope = self.inbox_scope(item.payload)
                    if self.inbox_claims.claim(item.id, scope):
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
                        if item.available_at > now() or not self.inbox_claims.available(item.id, scope):
                            continue
                        if scope[0] == "verification" and await self.verification_barrier(session, item):
                            continue
                        self.inbox_claims.claim(item.id, scope)
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
                return True
            # Bind during validation so Dispatcher need not round-trip SDK defaults.
            update = Update.model_validate(payload, context={"bot": self.tg.bot})
            await self.dispatcher.feed_update(self.tg.bot, update)
            async with self.db.transaction() as session:
                item = await session.get(Inbox, identifier)
                item.state = "done"
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
                item.error = failure(logger, self.service.settings, exc, "事件处理失败", f"update={identifier} lane={lane}", terminal=item.attempts >= 8)
                if item.attempts >= 8:
                    item.state = "failed"
        finally:
            self.current_update_id.reset(token)
            self.inbox_claims.release(identifier, scope)
            for event in self.inbox_ready.values():
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
                if not self.inbox_claims.available(item.id, self.inbox_scope(item.payload)):
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
                event = self.inbox_ready[lane]
                event.clear()
                if not await self.process_one(lane):
                    try:
                        await asyncio.wait_for(event.wait(), timeout=0.2)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.service.settings, exc, "Telegram 收件循环异常", f"lane={lane}", terminal=True)
                await asyncio.sleep(2)

    def start(self):
        if self.tg.bot:
            self.tasks = [
                asyncio.create_task(self.poll_loop(), name="telegram-poll"),
                *[asyncio.create_task(self.inbox_loop(), name=f"inbox-messages-{i}") for i in range(4)],
                *[asyncio.create_task(self.inbox_loop("control"), name=f"inbox-control-{i}") for i in range(2)],
                asyncio.create_task(self.inbox_loop("verification"), name="inbox-verification"),
                *[asyncio.create_task(self.service.action_loop("member"), name=f"actions-members-{i}") for i in range(4)],
                *[asyncio.create_task(self.service.action_loop("notification"), name=f"actions-notifications-{i}") for i in range(2)],
                asyncio.create_task(self.service.timeout_loop(), name="verification-timeouts"),
                asyncio.create_task(self.service.cleanup_loop(), name="verification-cleanup"),
                asyncio.create_task(self.service.progress.loop(), name="verification-progress"),
                asyncio.create_task(self.service.emoji_status.loop(), name="emoji-status"),
                asyncio.create_task(self.service.advertising.loop(), name="advertising-links"),
                asyncio.create_task(self.chat.run(), name="chat"),
            ]
            for task in self.tasks:
                task.add_done_callback(self.worker_finished)

    def worker_finished(self, task):
        if task.cancelled():
            return
        error = task.exception() or RuntimeError("常驻任务意外结束")
        self.last_error = failure(logger, self.service.settings, error, "后台任务退出", f"worker={task.get_name()}", terminal=True)

    async def stop(self):
        tasks = [*self.tasks, *self.ack_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.admin.ui.close()
        await self.chat.close()
        await self.tg.close()
