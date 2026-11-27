"""Classification and current exemptions for automatic member actions."""

from purrai.action_specs import (
    ACTION_POLICIES, AUTOMATIC_MEMBER_ACTIONS, DEFAULT_POLICY,
    NOTIFICATION_ACTIONS as NOTIFICATION_ACTIONS, TRUST_CANCELLABLE_ACTIONS as TRUST_CANCELLABLE_ACTIONS,
    ActionPolicySpec as ActionPolicySpec, MODERATION as MODERATION, VERIFICATION as VERIFICATION,
)
from purrai.models import Group, Whitelist


def administrator_protected_action(action):
    return bool(
        action.actor_id is None
        and action.group_id
        and action.user_id
        and action.user_id > 0
        and action.kind in AUTOMATIC_MEMBER_ACTIONS
        and not action.dedupe_key.startswith("cleanup:")
    )


class ActionPolicy:
    def __init__(self, settings):
        self.settings = settings

    async def moderation_paused(self, session, action):
        if action.actor_id is not None or action.dedupe_key.startswith(("cleanup:", "pending-message:")):
            return False
        if not ACTION_POLICIES.get(action.kind, DEFAULT_POLICY).pause_with_group:
            return False
        # A kick already sent to Telegram still needs its compensating unban.
        if action.kind in ("contact_kick", "identity_check") and action.payload.get("kick_until"):
            return False
        group = await session.get(Group, action.group_id)
        return bool(group and not group.enabled)

    async def automatic_exemption(self, session, action):
        if action.actor_id is not None or not action.user_id or action.dedupe_key.startswith("cleanup:"):
            return False
        scope = (
            "verification"
            if action.dedupe_key.startswith("pending-message:")
            else (ACTION_POLICIES.get(action.kind, DEFAULT_POLICY).exemption)
        )
        if not scope:
            return False
        if action.user_id in self.settings.superadmin_ids:
            return True
        white = await session.get(Whitelist, (action.group_id, action.user_id), populate_existing=True)
        if white and getattr(white, scope):
            return True
        if (action.kind in ("ai_ad_mute", "sample_ad_mute")
                or any(key in action.payload for key in ("advertisement_evidence", "ai_ad_evidence", "ai_keyword_evidence"))):
            from purrai.points_trust import member_trust
            return (await member_trust(session, action.group_id, action.user_id)).advertising
        return False
