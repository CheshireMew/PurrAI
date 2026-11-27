"""Admit incoming messages and match confirmed samples before model scheduling."""

import asyncio
import logging

from sqlalchemy import select

from purrai.advertisement_content import context_sensitive
from purrai.advertisement_similarity import rank
from purrai.advertisement_schemas import LOCAL_REVIEW_WINDOW_SECONDS
from purrai.advertisement_store import policy, revision, samples, wake_chat_reward
from purrai.message_buttons import inline_buttons
from purrai.message_identity import profile_name, message_sender
from purrai.message_parts import message_text
from purrai.models import Action, AdvertisementReview, Group, Violation, Whitelist, now
from purrai.points_trust import exempt_review
from purrai.policy_work import PolicyExecution, policy_work
from purrai.rules import ai_keyword_hints
from purrai.sender_profile import attach, biography
from purrai.shared_policy import effective_rules
from purrai.telegram import is_admin
from purrai.text_matching import message_content

logger = logging.getLogger(__name__)


class AdvertisementIntake:
    def __init__(self, db, telegram, settings, decisions, wakeup):
        self.db, self.tg, self.settings = db, telegram, settings
        self.decisions, self.wakeup = decisions, wakeup
        self.local_checks = set()

    async def observe(self, message, edited=False, update_id=0, *, chat_routing=False):
        if (
            message.get("new_chat_members")
            or message.get("left_chat_member")
            or message.get("_profile_event")
        ):
            return
        sender = message_sender(message)
        text = message_text(message)
        attached_content = bool(message_content(message)[1] or inline_buttons(message))
        group_id = message.get("chat", {}).get("id")
        if (
            message.get("chat", {}).get("type") not in ("group", "supergroup")
            or not sender.get("id")
            or message.get("from", {}).get("id") == self.tg.user_id
            or sender["id"] in self.settings.superadmin_ids
        ):
            return
        async with PolicyExecution(self.db) as session:
            group = await session.get(Group, group_id)
            cfg, _ = await policy(session, group_id)
            if not group or not group.approved or not group.enabled or not cfg.enabled:
                return
            from purrai.channel_moderation import trusted

            if await trusted(session, group_id, message):
                return
            white = await session.get(Whitelist, (group_id, sender["id"]))
            if white and white.moderation:
                return
            stamp = message.get("edit_date") or message["date"]
            previous = (
                await session.scalars(
                    select(AdvertisementReview).where(
                        AdvertisementReview.group_id == group_id,
                        AdvertisementReview.message_id == message["message_id"],
                    )
                )
            ).all()
            if not text.strip() and not attached_content and not previous:
                return
            digest = revision(message)
            if any(
                row.revision == digest or (row.event_at, row.update_id) > (stamp, update_id)
                for row in previous
            ):
                return
            # A local rule already handled this message; never infer an ad sample from
            # arbitrary violations such as a new member's link or a verification gate.
            violation = await session.scalar(
                select(Violation).where(
                    Violation.group_id == group_id, Violation.message_id == message["message_id"]
                )
            )
            handled = bool(violation and not edited)
            from purrai.chat_state import ignored_text
            from purrai import chat_commands

            _, commands = await chat_commands.load(session, group_id)
            from purrai.schemas import GroupSettings

            group_settings = GroupSettings.model_validate(group.settings)
            rules = await effective_rules(session, group_id)

            def check_command():
                ignored = group_settings.chat_ignored_texts + chat_commands.learned(
                    commands, group_settings, rules
                )
                name_hints = ai_keyword_hints("", rules, profile_name(sender), biography(message))
                return bool(
                    text == (message.get("text") or message.get("caption"))
                    and not attached_content
                    and not name_hints
                    and ignored_text(text, ignored, getattr(self.tg, "username", ""), rules=rules)
                )

            command = await policy_work(session, check_command)
            state = (
                "handled"
                if handled
                else "ignored"
                if (not text.strip() or command) and not attached_content
                else "pending"
            )
            if chat_routing and state == "pending":
                message = {**message, "_chat_routing": True}
            for row in previous:
                row.state, row.error = "superseded", "消息已编辑，旧判定失效"
            row = AdvertisementReview(
                group_id=group_id,
                message_id=message["message_id"],
                user_id=sender["id"],
                revision=digest,
                message=message,
                event_at=stamp,
                update_id=update_id,
                state=state,
                result={"source": "rules"}
                if handled
                else {"reason": "功能口令或空正文"}
                if state == "ignored"
                else {},
            )
            session.add(row)
            await session.flush()
            state = row.state
            if state == "pending" and await exempt_review(session, row):
                state = row.state
            identifier = row.id
            if edited:
                await wake_chat_reward(session, row)
        self.wakeup.set()
        # Local matching does not wait for a paid request already in progress.
        if state == "pending":
            await self.local(identifier)

    async def local(self, identifier):
        if identifier in self.local_checks:
            return False
        self.local_checks.add(identifier)
        try:
            return await self.check_samples(identifier)
        except Exception:
            async with self.db.transaction() as session:
                row = await session.get(AdvertisementReview, identifier)
                if row and row.state == "pending":
                    row.error = "本地匹配暂时无法核实成员状态，稍后重试"
                    row.local_after = row.available_at = now() + 5
            logger.warning("本地广告匹配暂未完成 review=%s", identifier)
            return False
        finally:
            self.local_checks.discard(identifier)

    async def check_samples(self, identifier):
        async with self.db.transaction() as session:
            row = await session.get(AdvertisementReview, identifier)
            if not row or row.state != "pending":
                return False
            # Without a model the message stays reviewable for new local samples.
            # Close that window with a final local decision so clean messages can earn points.
            offline_final = False
            if not self.settings.chat_ready and not row.attempts and now() - row.event_at >= LOCAL_REVIEW_WINDOW_SECONDS:
                offline_final = bool(
                    await session.scalar(
                        select(Action.id).where(
                            Action.dedupe_key == f"points:{row.group_id}:{row.message_id}",
                            Action.state == "pending",
                            Action.payload["operation"].as_string() == "chat",
                        )
                    )
                )
            valid, group, cfg, version = await self.decisions.valid(session, row, allow_expired=offline_final)
            if not valid:
                row.state, row.error = "cancelled", "消息、权限或审核设置已失效"
                await wake_chat_reward(session, row)
                return True
            if await exempt_review(session, row):
                return True
            # Mark the snapshot before releasing the writer. A newly saved sample
            # can make this row due again even while its old snapshot is being ranked.
            row.local_after = row.event_at + LOCAL_REVIEW_WINDOW_SECONDS
            if self.settings.chat_ready:
                row.local_after = min(row.local_after, now() + group.settings.get("bio_cache_seconds", 300))
            bank = await samples(session, row.group_id, direct_text=message_text(row.message))
            settings = dict(group.settings)
        message = dict(row.message)
        await attach(self.db, self.tg, message, settings)
        async with self.db.transaction() as session:
            current_row = await session.get(AdvertisementReview, identifier)
            if not current_row or current_row.state != "pending":
                return False
            current_row.message = message
        row.message = message
        match = await asyncio.to_thread(
            rank, message_text(row.message), bank, cfg, context_sensitive=context_sensitive(row.message)
        )
        if offline_final:
            matched = bool(match and match["matched"])
            exempt = (
                matched
                and row.user_id > 0
                and is_admin(await self.tg.call("get_chat_member", chat_id=row.group_id, user_id=row.user_id))
            )
            async with self.db.transaction() as session:
                final = await session.get(AdvertisementReview, identifier)
                if not final or final.state != "pending" or final.local_after == 0:
                    return False  # A concurrent settings/sample edit invalidated the snapshot.
                valid, _, _, latest_version = await self.decisions.valid(session, final, allow_expired=True)
                if not valid or version != latest_version:
                    final.state, final.error = "cancelled", "消息、权限或审核设置已失效"
                else:
                    final.state = "exempt" if exempt else "matched" if matched else "clean"
                    final.result = {
                        "source": "similarity",
                        "advertisement": matched,
                        "policy_version": version,
                        **(match or {}),
                    }
                final.updated_at = now()
                await wake_chat_reward(session, final)
                return True
        if not match or not match["matched"]:
            return False
        return await self.decisions.apply(
            row.id,
            {"advertisement": True, "scope": "text", "reason": "命中已确认广告样本"},
            version,
            match=match,
        )
