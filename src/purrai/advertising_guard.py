"""Resolve Telegram link types outside the verification workers and database writer."""

import asyncio
import copy
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from purrai.shared_policy import effective_rules
from purrai.action_queue import enqueue
from purrai.archive import mark_reasons
from purrai.advertising_patterns import GROUP_PREFIX, discussion
from purrai.group_link_identity import own_link_keys, remember_group_link
from purrai.diagnostics import failure
from purrai.models import Action, Group, Member, Whitelist, now, uid
from purrai.moderation_penalties import apply_penalty
from purrai.moderation_records import record_violation
from purrai.rules import message_content, normalize
from purrai.telegram import is_admin
from purrai.telegram_links import TelegramLink, allowed_link, extract_links, parse_link

logger = logging.getLogger(__name__)


def message_links(message):
    return extract_links(message_content(message)[1])


def link_reasons(message, group, rules, own_keys, evidence):
    text = message_content(message)[0]
    reporting = discussion(normalize(text))
    reasons = []
    for proof in evidence:
        link = TelegramLink(**proof["link"])
        if allowed_link(link, group.settings, rules, own_keys, group.id, message.get("chat", {}).get("username", "")):
            continue
        if proof.get("id") == group.id:
            continue
        if proof["type"] == "invite" and not group.settings.get("external_invite_ban", True):
            continue
        if proof["type"] != "invite" and not group.settings.get("external_group_ban", True):
            continue
        if proof["type"] not in ("invite", "group", "supergroup"):
            continue
        reasons.append(("举报或防骗引用外群链接，仅删除：" if reporting else GROUP_PREFIX) + link.key)
    return list(dict.fromkeys(reasons))


class AdvertisingGuard:
    def __init__(self, db, telegram, settings, trust):
        self.db, self.tg, self.settings, self.trust = db, telegram, settings, trust
        self.lock = asyncio.Lock()

    async def queue(self, session, group, member, message, update_id=0, edited=False):
        if not any(group.settings.get(key, True) for key in ("external_group_ban", "external_invite_ban")) or not message_links(message):
            return
        user_id = message["from"]["id"]
        if member is None:
            member = Member(group_id=group.id, user_id=user_id, joined_at=0)
            session.add(member)
            await session.flush()
        key = f"advertising-check:{group.id}:{message['message_id']}"
        action = await session.scalar(select(Action).where(Action.dedupe_key == key))
        if action and (action.payload.get("update_id", 0) > update_id > 0 or action.payload.get("message") == message):
            return
        payload = {"message": copy.deepcopy(message), "generation": member.generation, "edited": edited,
                   "cycle": uid(), "update_id": update_id}
        if action:
            action.payload, action.state, action.attempts, action.error, action.available_at = payload, "pending", 0, "", now()
        else:
            await enqueue(session, "advertisement_check", key, group.id, user_id, payload, priority=1)

    async def eligible(self, session, action):
        group = await session.get(Group, action.group_id)
        member = await session.get(Member, (action.group_id, action.user_id))
        white = await session.get(Whitelist, (action.group_id, action.user_id))
        if (not group or not group.approved or not group.enabled
            or not any(group.settings.get(key, True) for key in ("external_group_ban", "external_invite_ban"))
            or not member or not member.present or member.generation != action.payload["generation"]
            or action.user_id in self.settings.superadmin_ids or white and white.moderation):
            return None
        return group

    async def run_one(self, identifier=None):
        async with self.lock:
            async with self.db.transaction() as session:
                query = select(Action).where(Action.kind == "advertisement_check", Action.state == "pending", Action.available_at <= now())
                if identifier:
                    query = query.where(Action.id == identifier)
                action = await session.scalar(query.order_by(Action.created_at).limit(1))
                if not action:
                    return False
                group = await self.eligible(session, action)
                if not group:
                    action.state = "cancelled"
                    return True
                action.attempts += 1
                identifier, cycle, group_id, user_id = action.id, action.payload["cycle"], action.group_id, action.user_id
                incoming = copy.deepcopy(action.payload["message"])
                keys = await own_link_keys(session, group_id)
                rules = await effective_rules(session, group_id)
                settings = copy.deepcopy(group.settings)
            evidence, unknown, error, administrator, own = [], [], None, False, {}
            try:
                administrator = is_admin(await self.tg.call("get_chat_member", chat_id=group_id, user_id=user_id, request_timeout=3))
                if not administrator:
                    own = await self.tg.call("get_chat", chat_id=group_id, request_timeout=3)
                    if own.get("invite_link") and (link := parse_link(own["invite_link"])):
                        keys.append(link.key)
                    if own.get("username"):
                        keys.append("public:" + own["username"].lower())
                    for link in message_links(incoming):
                        if allowed_link(link, settings, rules, keys, group_id, incoming.get("chat", {}).get("username", "")):
                            continue
                        if link.kind == "invite":
                            # Bot API cannot inspect an arbitrary private invite.
                            # Apply the explicit invite policy only after loading our own identity.
                            if own.get("id") != group_id:
                                unknown.append(link.key)
                                continue
                            target = {"type": "invite"}
                        else:
                            try:
                                target = await self.tg.call("get_chat", chat_id=("@" + link.target if link.kind == "public" else int(link.target)), request_timeout=3)
                            except (TelegramBadRequest, TelegramForbiddenError):
                                unknown.append(link.key)
                                continue
                        if target.get("type") not in ("invite", "private", "channel", "group", "supergroup"):
                            unknown.append(link.key)
                            continue
                        evidence.append({"link": {"kind": link.kind, "target": link.target, "post": link.post},
                                         "type": target["type"], "id": target.get("id")})
            except Exception as exc:
                error = exc
            async with self.db.transaction() as session:
                action = await session.get(Action, identifier)
                if action.state != "pending" or action.payload["cycle"] != cycle:
                    return True
                group = await self.eligible(session, action)
                if not group:
                    action.state = "cancelled"
                elif administrator:
                    await self.trust.exempt_administrator(session, group_id, user_id)
                elif error:
                    failure(logger, self.settings, error, "Telegram 链接核实失败",
                            f"action={identifier} group={group_id}", terminal=action.attempts >= 8)
                    action.error = f"链接核实失败，未执行永久封禁：{type(error).__name__}"
                    action.available_at = now() + (error.retry_after if isinstance(error, TelegramRetryAfter) else min(300, 2 ** min(action.attempts, 8)))
                    if action.attempts >= 8 or isinstance(error, (TelegramBadRequest, TelegramForbiddenError)):
                        action.state = "failed"
                else:
                    if own.get("id") == group_id:
                        if own.get("invite_link"):
                            await remember_group_link(session, group_id, own["invite_link"])
                    keys = await own_link_keys(session, group_id)
                    rules = await effective_rules(session, group_id)
                    reasons = link_reasons(incoming, group, rules, keys, evidence)
                    if reasons:
                        await mark_reasons(session, incoming, action.payload.get("edited", False),
                                           action.payload.get("update_id", 0), reasons)
                        violation, changed = await record_violation(session, group_id=group_id, user_id=user_id,
                            message_id=incoming["message_id"], reasons=reasons,
                            excerpt=message_content(incoming)[0],
                            sender_name=" ".join(incoming["from"].get(key, "") for key in ("first_name", "last_name", "username")), message_violation=True)
                        if changed:
                            await apply_penalty(session, group, violation, await session.get(Member, (group_id, user_id)),
                                                evidence={"message": incoming, "links": evidence})
                    action.payload = {**action.payload, "evidence": evidence, "unresolved": unknown, "reasons": reasons}
                    action.state, action.error = "succeeded", "未能确认部分链接目标，未据此封禁" if unknown else ""
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
                failure(logger, self.settings, exc, "外群链接核实循环异常")
                await asyncio.sleep(1)
