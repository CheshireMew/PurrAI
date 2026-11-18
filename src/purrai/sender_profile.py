"""Fetch a sender's own public biography once, retaining availability and provenance."""

import asyncio
import hashlib
import json
from weakref import WeakValueDictionary

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from purrai.models import Meta, now
from purrai.telegram_health import request_purpose
from purrai.message_identity import message_sender, sender_chat

KEY = "sender_profile:"


def biography(message):
    profile = message.get("_sender_profile") or {}
    sender = message_sender(message)
    if sender_chat(message) or profile.get("user_id") != sender.get("id") or profile.get("status") != "available":
        return ""
    return profile.get("bio", "")


def digest(profile):
    return hashlib.sha256(json.dumps({key: profile.get(key) for key in ("user_id", "status", "bio")},
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class SenderProfiles:
    def __init__(self, db, telegram):
        self.db, self.tg = db, telegram
        self.locks = WeakValueDictionary()
        self.slots = asyncio.Semaphore(4)

    async def fetch(self, user_id, *, cache_seconds=300, fresh=False):
        # Coalesce concurrent group messages and action rechecks for one sender.
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            async with self.db.sessions() as session:
                row = await session.get(Meta, KEY + str(user_id))
                previous = json.loads(row.value) if row else {}
                pause_row = await session.get(Meta, KEY + "pause")
                pause_until = json.loads(pause_row.value).get("until", 0) if pause_row else 0
            age = now() - previous.get("checked_at", 0)
            if previous and (not fresh and age < cache_seconds and previous.get("status") in ("available", "empty")
                             or previous.get("retry_at", 0) > now()):
                return previous
            result = {"user_id": user_id, "bio": "", "status": "unavailable", "checked_at": now(), "error": "", "failures": 0}
            if pause_until > now():
                return {**result, "retry_at": pause_until, "error": "rate_limited"}
            pause = 0
            token = request_purpose.set("profile_lookup")
            try:
                async with self.slots:
                    async with asyncio.timeout(11):
                        profile = await self.tg.call("get_chat", chat_id=user_id, request_timeout=10)
                if not isinstance(profile, dict) or profile.get("id") != user_id or profile.get("type") != "private":
                    result["error"] = "profile_unavailable"
                else:
                    value = profile.get("bio") or ""
                    result.update(bio=value, status="available" if value else "empty")
            except TelegramRetryAfter as exc:
                pause, result["error"] = exc.retry_after, "rate_limited"
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                result["error"] = type(exc).__name__
            except (TimeoutError, ConnectionError, OSError) as exc:
                result["error"] = type(exc).__name__
            except Exception as exc:
                # The ordinary message still needs moderation if the optional
                # profile endpoint is unavailable. Never interpret that as clean.
                result["error"] = type(exc).__name__
            finally:
                request_purpose.reset(token)
            if result["status"] == "unavailable":
                result["failures"] = previous.get("failures", 0) + 1
                result["retry_at"] = now() + max(pause, min(300, 30 * 2 ** min(result["failures"] - 1, 4)))
            async with self.db.transaction() as session:
                if pause:
                    limited = await session.get(Meta, KEY + "pause")
                    until = max(now() + pause, json.loads(limited.value).get("until", 0) if limited else 0)
                    if limited:
                        limited.value = json.dumps({"until": until})
                    else:
                        session.add(Meta(key=KEY + "pause", value=json.dumps({"until": until})))
                row = await session.get(Meta, KEY + str(user_id))
                if row:
                    row.value = json.dumps(result, ensure_ascii=False)
                else:
                    session.add(Meta(key=KEY + str(user_id), value=json.dumps(result, ensure_ascii=False)))
            return result


def reader(db, telegram):
    if not hasattr(telegram, "sender_profiles"):
        telegram.sender_profiles = SenderProfiles(db, telegram)
    return telegram.sender_profiles


async def attach(db, telegram, message, settings, *, fresh=False):
    sender = message_sender(message)
    message.pop("_sender_profile", None)
    if (not telegram or not settings.get("bio_advertising_enabled", True) or sender_chat(message)
            or not sender.get("id") or sender["id"] <= 0 or sender.get("is_bot")):
        return
    message["_sender_profile"] = await reader(db, telegram).fetch(
        sender["id"], cache_seconds=settings.get("bio_cache_seconds", 300), fresh=fresh)


async def evidence_current(session, action, group, expected):
    """Do not execute biography-dependent penalties using stale profile data."""
    if not group or not group.settings.get("bio_advertising_enabled", True):
        return False
    execution = session.info["action_execution"]
    current = await execution.external(lambda: reader(execution.db, session.info["telegram"]).fetch(action.user_id, fresh=True))
    if current["status"] == "unavailable":
        raise ConnectionError("暂时无法复核用户简介，稍后重试")
    return bool(group.approved and group.enabled and group.settings.get("bio_advertising_enabled", True)
                and digest(current) == expected)
