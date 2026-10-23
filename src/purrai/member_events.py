"""Member presence and observed join transitions."""

import secrets

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.restriction_state import ban_matches, restriction_matches
from purrai.models import (
    Action,
    Group,
    Member,
    Verification,
    Whitelist,
    default_settings,
    now,
    uid,
)
from purrai.telegram import is_admin, is_present, permission_snapshot
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE


class MemberEvents:
    def __init__(self, db, telegram, settings, trust, moderation, emoji_status):
        self.db, self.tg, self.settings = db, telegram, settings
        self.trust, self.moderation, self.emoji_status = trust, moderation, emoji_status

    async def member_event(self, event: dict, bot_event=False, event_id=0):
        chat = event["chat"]
        from purrai.private_access import approved, discover

        if bot_event:
            async with self.db.transaction() as session:
                group = await discover(session, chat, self.settings.pending_group_limit)
                if not group or not group.approved:
                    return
        else:
            async with self.db.sessions() as session:
                if not await approved(session, chat["id"]):
                    return
        old, new = event["old_chat_member"], event["new_chat_member"]
        actor = event.get("from", {})
        manual_unmute = bool(
            not bot_event
            and old.get("status") == "restricted"
            and not old.get("can_send_messages", False)
            and is_present(new)
            and (
                new.get("status") == "member"
                or new.get("status") == "restricted"
                and new.get("can_send_messages")
            )
            and actor.get("id")
            and actor["id"] != self.tg.user_id
            and not actor.get("is_bot")
        )
        if manual_unmute and actor["id"] not in self.settings.superadmin_ids:
            # A human administrator's explicit event grants trust; automatic
            # expiry and another bot's changes must never create a whitelist.
            manual_unmute = is_admin(
                await self.tg.call("get_chat_member", chat_id=chat["id"], user_id=actor["id"])
            )
        async with self.db.transaction() as session:
            group = await session.get(Group, chat["id"])
            if not group:
                group = Group(
                    id=chat["id"],
                    title=chat.get("title", ""),
                    type=chat["type"],
                    settings=default_settings(),
                    enabled=False,
                    status="pending",
                    activated_at=0,
                )
                session.add(group)
                await session.flush()
            group.title = chat.get("title", group.title)
            old, new = event["old_chat_member"], event["new_chat_member"]
            if bot_event:
                group.status = "active" if group.enabled and is_admin(new) else "pending"
                if not is_present(new) or (
                    group.enabled
                    and not (
                        new.get("can_delete_messages")
                        and new.get("can_restrict_members")
                        and (not group.settings.get("join_requests") or new.get("can_invite_users"))
                    )
                ):
                    group.status = "error"
                return
            user = new["user"]
            member = await session.get(Member, (group.id, user["id"]))
            if member and event_id and member.last_event_id >= event_id:
                return
            if member and event_id:
                member.last_event_id = event_id
            if is_admin(new):
                await self.trust.exempt_administrator(session, group.id, user["id"])
                return
            if is_present(old) and not is_present(new):
                return await self.depart(session, group, member, user, new, event)
            if is_present(old) and is_present(new):
                return await self.update_role(
                    session, group, member, user, old, new, actor, event, event_id, manual_unmute
                )
            if is_present(old) or not is_present(new):
                return
            return await self.joined(session, group, member, user, new, event, event_id)

    async def depart(self, session, group, member, user, new, event):
        if member:
            owned = member.owned_restriction or {}
            if (
                event.get("from", {}).get("id") == self.tg.user_id
                and (owned.get("kind") in ("contact_kick", "verification_kick")
                     or owned.get("kind") == "ban" and owned.get("automatic_permanent"))
                and ban_matches(new, owned["until_date"])
            ):
                # Let the running action commit the accepted ban and finish
                # verification cleanup (or the legacy kick's compensating unban).
                return
            member.present = False
            retained_verification_mute = bool(
                owned.get("kind") in ("verification", "verification_failed")
                and not member.restriction_conflict
                and event.get("from", {}).get("id") == user["id"]
                and event["old_chat_member"].get("status") == "restricted"
                and restriction_matches(event["old_chat_member"], owned)
            )
            # Telegram can retain a member's permission exception after they
            # leave. Keep its provenance so the next captcha can lift our mute.
            if event.get("from", {}).get("id") != self.tg.user_id and not retained_verification_mute:
                member.owned_restriction = None
            for verification in (
                await session.scalars(
                    select(Verification).where(
                        Verification.group_id == group.id,
                        Verification.user_id == user["id"],
                        Verification.state.in_(ACTIVE),
                    )
                )
            ).all():
                verification.state = "left"
                await cleanup(session, verification)
        return

    async def update_role(
        self, session, group, member, user, old, new, actor, event, event_id, manual_unmute
    ):
        if is_admin(old) and new.get("status") == "member":
            if member:
                member.present = True
                member.owned_restriction, member.restriction_conflict = None, False
            return
        if manual_unmute:
            if member is None:
                member = Member(group_id=group.id, user_id=user["id"], joined_at=0)
                session.add(member)
                await session.flush()
            await self.trust.trust_member(session, member, actor["id"], "Telegram 群设置手动解除禁言")
            if event_id:
                member.last_event_id = event_id
            member.owned_restriction, member.restriction_conflict = None, False
            return
        # Changes made by another admin are never silently undone by PurrAI.
        if member and event.get("from", {}).get("id") != self.tg.user_id:
            member.restriction_conflict = True
        return

    async def joined(self, session, group, member, user, new, event, event_id):
        if not group.enabled or event["date"] + 1 <= group.activated_at:
            return
        if member:
            approved = await session.scalar(
                select(Verification).where(
                    Verification.group_id == group.id,
                    Verification.user_id == user["id"],
                    Verification.generation == member.generation,
                    Verification.flow == "join_request",
                    Verification.state.in_(("passed", "releasing")),
                )
            )
            if approved and not approved.previous_permissions.get("entry_consumed"):
                approved.previous_permissions = {**approved.previous_permissions, "entry_consumed": True}
                approved.state, approved.error, approved.updated_at = "passed", "", now()
                member.present, member.joined_at, member.passed_at = True, event["date"], now()
                member.join_observed = True
                member.last_event_id, member.request_until = event_id, 0
                await cleanup(session, approved)
                pending = (
                    await session.scalars(
                        select(Action).where(
                            Action.kind == "release",
                            Action.state == "pending",
                            Action.payload["verification_id"].as_string() == approved.id,
                        )
                    )
                ).all()
                for action in pending:
                    action.state = "succeeded"
                await self.emoji_status.queue(session, group, user)
                await self.moderation.profile_on_join(session, group, member, user)
                return
        if member and member.present and member.joined_at == event["date"]:
            return
        inherited_mute, inherited_permissions = None, None
        owned = member.owned_restriction if member else None
        if (
            owned and owned.get("kind") in ("verification", "verification_failed")
            and owned.get("verification_id")
            and not member.restriction_conflict
            and new.get("status") == "restricted"
            and restriction_matches(new, owned)
        ):
            source = await session.get(Verification, owned.get("verification_id"))
            if (
                source and source.group_id == group.id and source.user_id == user["id"]
                and source.generation == member.generation and source.flow == "joined"
                and (
                    source.state in (*ACTIVE, "left")
                    or owned["kind"] == "verification_failed"
                    and source.state == "failed" and source.failure_action == "permanent_mute"
                )
            ):
                inherited_mute = dict(owned)
                inherited_permissions = dict(source.previous_permissions)
        if member is None:
            member = Member(group_id=group.id, user_id=user["id"])
            session.add(member)
        member.joined_at = event["date"]
        member.join_observed = True
        member.last_event_id = event_id
        member.present = True
        member.generation = uid()
        member.passed_at = 0
        member.observed_messages = 0
        member.owned_restriction = None
        member.restriction_conflict = False
        # Invalidate any earlier join's captcha without touching current membership permissions.
        for prior in (
            await session.scalars(
                select(Verification).where(
                    Verification.group_id == group.id,
                    Verification.user_id == user["id"],
                    Verification.state.in_(ACTIVE),
                )
            )
        ).all():
            prior.state = "superseded"
            await cleanup(session, prior)
        white = await session.get(Whitelist, (group.id, user["id"]))
        if is_admin(new) or user["id"] in self.settings.superadmin_ids:
            member.passed_at = now()
            return
        if not white or not white.moderation:
            await self.emoji_status.queue(session, group, user)
        if await self.moderation.profile_on_join(session, group, member, user):
            return
        if (white and white.verification) or not group.settings["verification_enabled"]:
            member.passed_at = now()
            return
        verification = Verification(
            id=uid(),
            group_id=group.id,
            user_id=user["id"],
            user_name=user.get("first_name", str(user["id"])),
            generation=member.generation,
            state="muting",
            nonce=secrets.token_hex(4),
            attempts=0,
            input_digits="",
            deadline=now() + group.settings["verification_timeout"],
            previous_permissions=(
                inherited_permissions if inherited_permissions is not None
                else permission_snapshot(new) if new["status"] == "restricted" else {}
            ),
        )
        session.add(verification)
        await session.flush()
        if inherited_mute:
            # Transfer only a matching verification restriction to this join;
            # retain the snapshot from before the first mute, not the mute itself.
            member.owned_restriction = {
                **inherited_mute, "kind": "verification", "verification_id": verification.id,
            }
        await enqueue(
            session,
            "verify_mute",
            f"mute:{verification.id}",
            group.id,
            user["id"],
            {"verification_id": verification.id},
            priority=0,
        )
