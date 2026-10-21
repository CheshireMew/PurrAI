"""Automatic moderation actions selected by the action dispatcher."""


class AutomaticActions:
    def __init__(self, restrictions):
        self.restrictions = restrictions

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
