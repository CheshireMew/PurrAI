"""Classification and current exemptions for automatic member actions."""

from dataclasses import dataclass

from purrai.models import Group, Whitelist


@dataclass(frozen=True)
class ActionPolicySpec:
    administrator_protected: bool = False
    cancel_on_trust: bool = False
    pause_with_group: bool = False
    exemption: str = ""
    notification: bool = False


VERIFICATION = ActionPolicySpec(True, True, exemption="verification")
MODERATION = ActionPolicySpec(True, True, True, "moderation")
ACTION_POLICIES = {
    "verify_mute": VERIFICATION,
    "challenge": VERIFICATION,
    "progress": VERIFICATION,
    "verification_failure": VERIFICATION,
    "ban": VERIFICATION,
    "release": ActionPolicySpec(administrator_protected=True),
    "profile_mute": MODERATION,
    "emoji_status_mute": MODERATION,
    "contact_kick": MODERATION,
    "contact_ban": MODERATION,
    "emoji_status_ban": MODERATION,
    "advertisement_ban": MODERATION,
    "advertisement_check": MODERATION,
    "rule_mute": MODERATION,
    "moderation_mute": MODERATION,
    "delete": MODERATION,
    "emoji_status_check": ActionPolicySpec(True, True, True),
    "warn": ActionPolicySpec(administrator_protected=True, pause_with_group=True, notification=True),
    "admin_notify": ActionPolicySpec(notification=True),
    "archive_export": ActionPolicySpec(notification=True),
    "join_link": ActionPolicySpec(notification=True),
}
DEFAULT_POLICY = ActionPolicySpec()
AUTOMATIC_MEMBER_ACTIONS = tuple(
    kind for kind, spec in ACTION_POLICIES.items() if spec.administrator_protected
)
TRUST_CANCELLABLE_ACTIONS = tuple(kind for kind, spec in ACTION_POLICIES.items() if spec.cancel_on_trust)
NOTIFICATION_ACTIONS = tuple(kind for kind, spec in ACTION_POLICIES.items() if spec.notification)


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
        if action.kind == "contact_kick" and action.payload.get("kick_until"):
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
        return bool(white and getattr(white, scope))
