"""Compare configured protected identities with current Telegram profile evidence."""
import asyncio
import hashlib
import io
import json
import re
import time
import unicodedata

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from PIL import Image, ImageChops, ImageOps, ImageStat
from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import Action, Group, Member, Whitelist, now
from purrai.schemas import GroupSettings
from purrai.sender_identity import sender_name
from purrai.telegram import is_admin, is_present


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold()
                   if not c.isspace() and unicodedata.category(c) != "Cf")


def config_for(settings):
    values = GroupSettings.model_validate(settings).model_dump()
    return {key: value for key, value in values.items() if key.startswith("identity_")}


def matching_name(name, config):
    value = normalized(name)
    if config["identity_parentheses"]:
        value = re.sub(r"(?:\([^()]*\))+$", "", value)
    return bool(value and value in [normalized(n) for n in config["identity_names"]])


def candidate(user, config):
    return (config["identity_enabled"] and user.get("id", 0) > 0 and not user.get("is_bot")
            and user["id"] != config["identity_user_id"] and matching_name(sender_name(user), config))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def photo_pixels(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 10_000_000:
            raise ValueError("头像图片尺寸过大，未执行处罚")
        return ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"), (64, 64), method=Image.Resampling.LANCZOS)


def same_photo(left, right):
    # Small compression/resizing differences are tolerated, not semantic resemblance.
    a, b = photo_pixels(left), photo_pixels(right)
    if a.tobytes() == b.tobytes():
        return True
    if min(max(ImageStat.Stat(a).stddev), max(ImageStat.Stat(b).stddev)) < 10:
        return False
    return max(ImageStat.Stat(ImageChops.difference(a, b)).rms) <= 6


def evidence_current(group, action):
    saved = action.payload.get("identity_evidence")
    return bool(saved and config_for(group.settings) == saved["config"]
                and saved["config"]["identity_enabled"]
                and action.user_id != saved["config"]["identity_user_id"])


class IdentityProtection:
    def __init__(self, db, telegram, settings):
        self.db, self.tg, self.settings = db, telegram, settings
        self.request_lock = asyncio.Lock()
        self.next_request = 0.0
        self.interval = 2.0
        self.reference_cache = {}
        self.photo_cache = {}

    async def limited(self, operation):
        async with self.request_lock:
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            try:
                return await operation()
            except TelegramRetryAfter as exc:
                self.next_request = time.monotonic() + exc.retry_after + 1
                raise
            finally:
                self.next_request = max(self.next_request, time.monotonic() + self.interval)

    async def call(self, method, **kwargs):
        return await self.limited(lambda: self.tg.call(method, **kwargs))

    async def queue(self, group_id, user, *, force=False):
        if not user or user.get("id") in self.settings.superadmin_ids:
            return
        async with self.db.transaction() as session:
            group = await session.get(Group, group_id)
            if not group or not group.approved or not group.enabled:
                return
            config = config_for(group.settings)
            if not candidate(user, config):
                return
            white = await session.get(Whitelist, (group_id, user["id"]))
            if white and white.moderation:
                return
            member = await session.get(Member, (group_id, user["id"]))
            if member is None:
                member = Member(group_id=group_id, user_id=user["id"], present=True, joined_at=0)
                session.add(member)
                await session.flush()
            if not member.present:
                return
            key = f"identity:{group_id}:{user['id']}:{member.generation}"
            action = await session.scalar(select(Action).where(Action.dedupe_key == key))
            signature = fingerprint([config, sender_name(user)])
            if action:
                if action.state == "pending" or (not force and action.payload.get("signature") == signature and now() - action.updated_at < 600):
                    return
                action.state, action.attempts, action.error, action.available_at = "pending", 0, "", now()
                action.payload = {"generation": member.generation, "signature": signature}
            else:
                await enqueue(session, "identity_check", key, group_id, user["id"],
                              {"generation": member.generation, "signature": signature}, priority=1)

    async def profile(self, user_id):
        result = {"id": user_id, "bio": "", "photo": None, "unavailable": []}
        try:
            profile = await self.call("get_chat", chat_id=user_id)
            if profile.get("type") == "private" and profile.get("id") == user_id:
                result["bio"] = normalized(profile.get("bio", ""))
            else:
                result["unavailable"].append("bio")
        except (TelegramBadRequest, TelegramForbiddenError):
            result["unavailable"].append("bio")
        try:
            photos = await self.call("get_user_profile_photos", user_id=user_id, limit=1)
            if photos.get("photos"):
                result["photo"] = max(photos["photos"][0], key=lambda item: item["width"] * item["height"])
        except (TelegramBadRequest, TelegramForbiddenError):
            result["unavailable"].append("photo")
        return result

    async def image(self, photo):
        key = photo.get("file_unique_id") or photo["file_id"]
        if key not in self.photo_cache:
            file = await self.call("get_file", file_id=photo["file_id"])
            if file.get("file_size", 0) > 5_000_000:
                raise ValueError("头像文件过大，未执行处罚")
            data = await self.limited(lambda: self.tg.bot.download_file(file["file_path"], timeout=15))
            raw = data.getvalue()
            if len(raw) > 5_000_000:
                raise ValueError("头像文件过大，未执行处罚")
            if len(self.photo_cache) >= 64:
                self.photo_cache.pop(next(iter(self.photo_cache)))
            self.photo_cache[key] = raw
        return self.photo_cache[key]

    async def inspect(self, group_id, user_id, config):
        current = await self.call("get_chat_member", chat_id=group_id, user_id=user_id)
        if not is_present(current) or is_admin(current) or not candidate(current.get("user", {}), config):
            return {"matched": [], "outcome": "not_candidate"}
        target = await self.profile(user_id)
        protected_id = config["identity_user_id"]
        cached = self.reference_cache.get(protected_id)
        if not cached or now() - cached[0] >= 600:
            reference = await self.profile(protected_id)
            self.reference_cache[protected_id] = (now(), reference)
        else:
            reference = cached[1]
        matched = []
        if config["identity_match_bio"] and target["bio"] and target["bio"] == reference["bio"]:
            matched.append("bio")
        a, b = target["photo"], reference["photo"]
        if not matched and config["identity_match_photo"] and a and b:
            same = a.get("file_unique_id") and a.get("file_unique_id") == b.get("file_unique_id")
            if same or await asyncio.to_thread(same_photo, await self.image(a), await self.image(b)):
                matched.append("photo")
        return {"matched": matched, "outcome": "confirmed" if matched else "not_confirmed",
                "name": sender_name(current["user"]), "reference_id": protected_id,
                "bio_digest": fingerprint(target["bio"]) if target["bio"] else "",
                "target_photo": a.get("file_unique_id", "") if a else "",
                "reference_photo": b.get("file_unique_id", "") if b else "",
                "unavailable": {"target": target["unavailable"], "reference": reference["unavailable"]}}

    async def execute(self, session, action, group, verification, member, restrictions):
        if group and member and action.payload.get("kick_until"):
            await restrictions.kick_member(session, group, member, action, "identity_kick", allow_existing_restriction=True)
            return
        if not group or not member or not member.present:
            action.state = "cancelled"
            return
        config = config_for(group.settings)
        if not config["identity_enabled"] or action.user_id == config["identity_user_id"]:
            action.state = "cancelled"
            return
        result = await session.info["action_execution"].external(lambda: self.inspect(group.id, action.user_id, config))
        if config_for(group.settings) != config:
            action.state, action.error = "cancelled", "防冒充设置已变化，取消旧核验"
            return
        action.payload = {**action.payload, "identity_evidence": {"config": config, "checked_at": now(), **result}}
        if not result["matched"]:
            action.reason = "资料核验未确认冒充，未执行处罚"
            return
        action.reason = "同名账号的" + "、".join("头像" if k == "photo" else "简介" for k in result["matched"]) + "与受保护账号一致"
        if config["identity_action"] == "ban":
            await restrictions.permanent_ban(session, group, member, action)
        else:
            await restrictions.kick_member(session, group, member, action, "identity_kick", allow_existing_restriction=True)
