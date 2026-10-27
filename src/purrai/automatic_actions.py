"""Automatic moderation actions selected by the action dispatcher."""

from sqlalchemy import select

from purrai.advertising_guard import link_reasons
from purrai.advertising_patterns import advertising_reasons, is_ban_reason, source_titles
from purrai.group_link_identity import own_channel_id, own_link_keys
from purrai.models import Rule
from purrai.rules import message_content, normalize
from purrai.serialization import row_dict
from purrai.telegram_sources import channel_sources, external_channel_reasons


class AutomaticActions:
    def __init__(self, restrictions):
        self.restrictions = restrictions

    async def contact_ban(self, session, action, group, verification, member):
        if not group.enabled or not member or not member.present:
            action.state = "cancelled"
            return
        await self.restrictions.permanent_ban(session, group, member, action)

    async def advertisement_ban(self, session, action, group, verification, member):
        if not member or not member.present:
            action.state = "cancelled"
            return
        evidence = action.payload.get("ad_evidence") or {}
        message = evidence.get("message", {})
        reasons = advertising_reasons(normalize(message_content(message)[0]),
                                      [normalize(title) for title in source_titles(message)]) if group.settings.get("advertising_ban", True) else []
        rules = [row_dict(row) for row in await session.scalars(select(Rule).where(Rule.group_id == group.id))]
        reasons += link_reasons(message, group, rules, await own_link_keys(session, group.id), evidence.get("links", []))
        if group.settings.get("external_group_ban", True):
            reasons += external_channel_reasons(channel_sources(message), group.id, group.settings, rules,
                                                linked_channel_id=await own_channel_id(session, group.id))
        if not any(is_ban_reason(reason) for reason in reasons):
            action.state, action.error = "cancelled", "广告或链接策略已变化，未执行永久封禁"
            return
        await self.restrictions.permanent_ban(session, group, member, action)

    async def emoji_ban(self, session, action, group, verification, member):
        await self.restrictions.ban_emoji_status(session, group, member, action)

    async def contact_kick(self, session, action, group, verification, member):
        if not group.enabled or not member or not member.present and not action.payload.get("kick_until"):
            action.state = "cancelled"
            return
        await self.restrictions.kick_member(session, group, member, action, "contact_kick")

    async def legacy_mute(self, session, action, group, verification, member):
        action.state = "cancelled"
        return

    async def emoji_mute(self, session, action, group, verification, member):
        await self.restrictions.mute_emoji_status(session, group, member, action)

    async def profile_mute(self, session, action, group, verification, member):
        await self.restrictions.permanent_profile_mute(session, group, member, action, "profile")

    async def rule_mute(self, session, action, group, verification, member):
        if not group.enabled:
            action.state = "cancelled"
            return
        await self.restrictions.permanent_profile_mute(session, group, member, action, "rule")
