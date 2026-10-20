"""Telegram sticker metadata, open emoji selection and quiet group reminders."""

import asyncio
import json
import random
import re
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import select

from purrai.models import Group, Meta, now

DEFAULT_CHAT_PACKS = (
    "myadestes", "Beach_Bat_Responses_by_fStikBot", "poi466", "LLMG_GIF", "cookprivet",
    "tofupack_by_fStikBot", "catvideopack_by_fStikBot", "superCheshire", "sumyy3", "HMS_Cheshire",
    "p1_wmtz1349120938_by_WuMingv2Bot", "Cheshire_the_kitten", "sticker_ab2f7c92_by_moe_sticker_bot",
    "sese11_by_WuMingv2Bot", "p0_wmtz1678979585_by_WuMingv2Bot", "p0_wmtz1296407908_by_WuMingv2Bot",
    "GreatCheshire", "BigCheshire", "goodCheshire", "AwesomeCheshireCat",
)
DEFAULT_ANTIFRAUD_PACKS = ("FTX888",)


def emoji_key(value):
    # Presentation selectors are interchangeable; joiners, skin tones and flags are not.
    return value.strip().translate({0xFE0E: None, 0xFE0F: None})


def sticker_emojis(packs, recent=()):
    """Discover every available tag, including new or compound emojis, without a fixed vocabulary."""
    return sorted({emoji_key(s.get("emoji", "")) for pack in packs for s in pack.get("stickers", [])
                   if s["file_unique_id"] not in recent and emoji_key(s.get("emoji", ""))})


def pack_name(value):
    value = value.strip()
    if "://" in value or value.startswith("t.me/"):
        parsed = urlsplit(value if "://" in value else "https://" + value)
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname not in ("t.me", "www.t.me") or len(parts) != 2 or parts[0] != "addstickers":
            raise ValueError("聊天贴纸请填写 t.me/addstickers 链接或贴纸包短名称")
        value = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
        raise ValueError("贴纸包短名称只能包含字母、数字和下划线，最长 64 个字符")
    return value


def sticker_text(message):
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text and message.get("sticker"):
        emoji = (message["sticker"].get("emoji") or "")[:24]
        return f"[贴纸{(' ' + emoji) if emoji else ''}]"
    return text


class Stickers:
    def __init__(self, db, telegram):
        self.db, self.tg = db, telegram
        self.locks = {}
        self.refresh_lock = asyncio.Lock()

    def lock(self, group_id):
        return self.locks.setdefault(group_id, asyncio.Lock())

    async def state(self, session, group_id):
        row = await session.get(Meta, f"chat_stickers:{group_id}")
        return row, json.loads(row.value) if row else {}

    def save(self, session, row, group_id, state):
        value = json.dumps(state, ensure_ascii=False)
        if row:
            row.value = value
        else:
            session.add(Meta(key=f"chat_stickers:{group_id}", value=value))

    async def activity(self, session, group_id, topic_id, interval):
        row, state = await self.state(session, group_id)
        state.setdefault("reminder_started", now())
        state.setdefault("reminder_next", now() + interval)
        state["messages"] = min(state.get("messages", 0) + 1, 1000)
        state.update(last_activity=now(), topic_id=topic_id)
        self.save(session, row, group_id, state)

    def cache_key(self, name):
        return f"sticker_pack:{self.tg.user_id}:{name.lower()}"

    async def catalog(self, names, session=None):
        if not names:
            return []
        if session is None:
            async with self.db.sessions() as session:
                return await self.catalog(names, session)
        rows = (await session.scalars(select(Meta).where(
            Meta.key.in_([self.cache_key(name) for name in names]),
        ))).all()
        return [json.loads(row.value) for row in rows]

    async def refresh(self, names):
        """Only metadata is fetched. Failed packs back off without hiding working ones."""
        async with self.refresh_lock:
            current = {item["name"].lower(): item for item in await self.catalog(names)}
            semaphore = asyncio.Semaphore(2)

            async def fetch(name):
                old = current.get(name.lower(), {})
                if now() < old.get("retry_at", 0):
                    return
                async with semaphore:
                    try:
                        pack = await self.tg.call("get_sticker_set", name=name, request_timeout=3)
                        if pack.get("sticker_type") != "regular":
                            raise ValueError("需要普通贴纸包，不能使用自定义表情包或面具包")
                        items = [{"file_id": s["file_id"], "file_unique_id": s["file_unique_id"],
                                  "emoji": s.get("emoji", ""), "set_name": pack["name"]}
                                 for s in pack["stickers"]]
                        value = {"name": name, "stickers": items, "error": "", "retry_at": now() + 86400}
                    except TelegramRetryAfter as exc:
                        value = {**old, "name": name, "error": "Telegram 限流，稍后重试",
                                 "retry_at": now() + max(60, exc.retry_after)}
                    except Exception:
                        value = {**old, "name": name, "error": "贴纸包暂时无法读取或不是普通贴纸包",
                                 "retry_at": now() + 3600}
                    async with self.db.transaction() as session:
                        row = await session.get(Meta, self.cache_key(name))
                        if row:
                            row.value = json.dumps(value, ensure_ascii=False)
                        else:
                            session.add(Meta(key=self.cache_key(name), value=json.dumps(value, ensure_ascii=False)))

            await asyncio.gather(*(fetch(name) for name in dict.fromkeys(names)))

    async def options(self, session, group_id, cfg):
        _, state = await self.state(session, group_id)
        if (not cfg.chat_stickers or not cfg.chat_sticker_packs
            or now() < state.get("last_sticker", 0) + cfg.chat_sticker_interval
            or now() < state.get("blocked_until", 0)):
            return []
        packs = await self.catalog(cfg.chat_sticker_packs, session)
        return sticker_emojis(packs, state.get("recent", []))

    async def choose(self, names, emoji=None, recent=()):
        packs = await self.catalog(names)
        choices = [[s for s in pack.get("stickers", []) if s["file_unique_id"] not in recent
                    and (emoji is None or emoji_key(s.get("emoji", "")) == emoji_key(emoji))] for pack in packs]
        choices = [items for items in choices if items]
        # Equal chance per pack prevents the largest pack dominating every conversation.
        return random.choice(random.choice(choices)) if choices else None

    async def sent(self, session, group_id, sticker):
        row, state = await self.state(session, group_id)
        state["last_sticker"] = now()
        state["recent"] = [*state.get("recent", []), sticker["file_unique_id"]][-12:]
        state.pop("error", None)
        self.save(session, row, group_id, state)

    async def rejected(self, group_id, message="贴纸发送被 Telegram 拒绝，已暂用文字回复"):
        async with self.db.transaction() as session:
            row, state = await self.state(session, group_id)
            state.update(blocked_until=now() + 3600, error=message)
            self.save(session, row, group_id, state)

    def reminder_due(self, group, cfg, state):
        anchor = state.get("reminder_attempted", state.get("reminder_started", 0))
        scheduled = state.get("reminder_next", now() + cfg.chat_antifraud_interval)
        if anchor and state.get("reminder_state") != "limited":
            scheduled = anchor + cfg.chat_antifraud_interval
        return bool(
            group.approved and group.enabled and group.status == "active" and cfg.chat_enabled
            and cfg.chat_antifraud and cfg.chat_antifraud_packs
            and now() >= scheduled
            and now() >= state.get("blocked_until", 0)
            and now() >= state.get("last_sticker", 0) + max(30, cfg.chat_sticker_interval)
            and state.get("messages", 0) >= 50 and now() - state.get("last_activity", 0) <= 600
            and 9 <= datetime.now(ZoneInfo("Asia/Shanghai")).hour < 23
        )

    async def reminder(self, group_id):
        from purrai.archive import capture
        from purrai.schemas import GroupSettings

        async with self.lock(group_id):
            async with self.db.transaction() as session:
                group = await session.get(Group, group_id)
                cfg = GroupSettings.model_validate(group.settings)
                row, state = await self.state(session, group_id)
                if not self.reminder_due(group, cfg, state):
                    return False
                topic_id = state.get("topic_id", 0)
                recent = state.get("reminder_recent", [])
            sticker = await self.choose(cfg.chat_antifraud_packs, recent=recent[-1:])
            if not sticker:
                return False
            # Commit the attempt before sending. A crash/unknown response never causes a resend.
            async with self.db.transaction() as session:
                group = await session.get(Group, group_id)
                cfg = GroupSettings.model_validate(group.settings)
                row, state = await self.state(session, group_id)
                if (not self.reminder_due(group, cfg, state) or sticker["set_name"].lower() not in {
                    name.lower() for name in cfg.chat_antifraud_packs
                }):
                    return False
                state.update(reminder_next=now() + cfg.chat_antifraud_interval,
                             reminder_attempted=now(), reminder_state="sending")
                self.save(session, row, group_id, state)
            try:
                sent = await self.tg.call("send_sticker", chat_id=group_id, sticker=sticker["file_id"],
                                          **({"message_thread_id": topic_id} if topic_id else {}))
            except TelegramRetryAfter as exc:
                async with self.db.transaction() as session:
                    row, state = await self.state(session, group_id)
                    attempts = state.get("reminder_attempts", 0) + 1
                    state.update(reminder_attempts=attempts, reminder_state="limited")
                    if attempts < 3:
                        state["reminder_next"] = now() + max(60, exc.retry_after)
                    else:
                        state["reminder_attempts"] = 0
                    self.save(session, row, group_id, state)
                return False
            except Exception:
                await self.rejected(group_id, "防诈贴纸发送未完成，本轮不自动重发")
                return False
            async with self.db.transaction() as session:
                await self.sent(session, group_id, sticker)
                row, state = await self.state(session, group_id)
                state.update(messages=0, reminder_state="done", reminder_attempts=0,
                             reminder_recent=[*recent, sticker["file_unique_id"]][-6:])
                self.save(session, row, group_id, state)
                await capture(session, {
                    "chat": {"id": group_id, "type": group.type, "title": group.title},
                    "from": {"id": self.tg.user_id, "is_bot": True, "first_name": cfg.chat_name},
                    "message_id": sent["message_id"], "date": sent.get("date", int(now())),
                    "sticker": sticker, **({"message_thread_id": topic_id} if topic_id else {}),
                })
            return True

    async def cycle(self):
        from purrai.schemas import GroupSettings

        async with self.db.sessions() as session:
            groups = (await session.scalars(select(Group))).all()
        names, active = set(), []
        for group in groups:
            cfg = GroupSettings.model_validate(group.settings)
            if group.approved and cfg.chat_enabled:
                if cfg.chat_stickers:
                    names.update(cfg.chat_sticker_packs)
                if cfg.chat_antifraud and group.enabled and group.status == "active":
                    names.update(cfg.chat_antifraud_packs)
                    active.append(group.id)
        await self.refresh(sorted(names))
        for group_id in active:
            await self.reminder(group_id)

    async def status(self, cfg, group_id):
        names = list(dict.fromkeys([*cfg.chat_sticker_packs, *cfg.chat_antifraud_packs]))
        packs = await self.catalog(names)
        chat_names = {name.lower() for name in cfg.chat_sticker_packs}
        chat_packs = [item for item in packs if item["name"].lower() in chat_names]
        loaded = {item["name"].lower() for item in packs if item.get("stickers")}
        async with self.db.sessions() as session:
            _, state = await self.state(session, group_id)
        return {"ready": len(loaded), "total": len(names),
                "emoji_count": len(sticker_emojis(chat_packs)),
                "unavailable": [item["name"] for item in packs if item.get("error")],
                "error": state.get("error", "")}
