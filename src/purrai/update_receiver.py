"""Telegram polling and atomic persistence of received updates and offsets."""

import asyncio
import logging

from purrai.diagnostics import failure
from purrai.group_link_identity import remember_update_links
from purrai.models import Inbox, Meta, now
from purrai.private_access import approved, discover, update_group_id
from purrai.telegram import telegram_data
from purrai.traffic import RateLimit

logger = logging.getLogger(__name__)
ALLOWED_UPDATES = ["chat_member", "my_chat_member", "chat_join_request", "message", "edited_message", "callback_query"]


class UpdateReceiver:
    def __init__(self, db, telegram, settings, ready_events):
        self.db, self.tg, self.settings = db, telegram, settings
        self.ready_events = ready_events
        self.last_poll, self.last_error = 0.0, ""
        self.menu_configured = False
        self.control_limit = RateLimit(per_key=120, total=600)

    async def persist(self, updates: list[dict]):
        if not updates:
            return
        async with self.db.transaction() as session:
            previous_offset = await session.get(Meta, "poll_offset")
            highwater = int(previous_offset.value) if previous_offset else -1
            for payload in updates:
                from purrai.group_migration import receive_migration
                if await receive_migration(session, payload):
                    continue
                received_event = next((payload[field] for field in (
                    "message", "edited_message", "chat_member", "my_chat_member", "chat_join_request"
                ) if field in payload), {})
                if payload["update_id"] < highwater and received_event.get("date", now()) < now() - 7 * 86400:
                    continue  # Do not resurrect expired receipts; recent delayed originals still record normally.
                bot_event = payload.get("my_chat_member")
                if bot_event:
                    await discover(session, bot_event["chat"], self.settings.pending_group_limit)
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
                        await capture(session, message, "edited_message" in payload, payload["update_id"], settings=self.settings)
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
        for event in self.ready_events.values():
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
                        failure(logger, self.settings, exc, "Telegram 命令菜单更新失败")
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
                self.last_error = failure(logger, self.settings, exc, "Telegram 轮询失败")
                self.tg.connected = False
                await asyncio.sleep(5)
