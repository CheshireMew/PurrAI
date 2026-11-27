"""Action classification shared by scheduling and current-state policy."""

from dataclasses import dataclass


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
    "linked_channel_unpin": ActionPolicySpec(pause_with_group=True, notification=True),
    "channel_ad_ban": MODERATION,
    "identity_check": MODERATION,
    "verify_mute": VERIFICATION,
    "challenge": VERIFICATION,
    "subscription_check": VERIFICATION,
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
    "spam_mute": MODERATION,
    "ai_ad_mute": MODERATION,
    "sample_ad_mute": MODERATION,
    "moderation_mute": MODERATION,
    "delete": MODERATION,
    "emoji_status_check": ActionPolicySpec(True, True, True),
    "warn": ActionPolicySpec(administrator_protected=True, pause_with_group=True, notification=True),
    "admin_notify": ActionPolicySpec(notification=True),
    "archive_export": ActionPolicySpec(notification=True),
    "join_link": ActionPolicySpec(notification=True),
    "owner_alert": ActionPolicySpec(notification=True),
    "auto_reply": ActionPolicySpec(notification=True),
    "points_message": ActionPolicySpec(notification=True),
    "lottery_notice": ActionPolicySpec(notification=True),
    "group_menu": ActionPolicySpec(notification=True),
}
DEFAULT_POLICY = ActionPolicySpec()
AUTOMATIC_MEMBER_ACTIONS = tuple(
    kind for kind, spec in ACTION_POLICIES.items() if spec.administrator_protected
)
TRUST_CANCELLABLE_ACTIONS = tuple(kind for kind, spec in ACTION_POLICIES.items() if spec.cancel_on_trust)
NOTIFICATION_ACTIONS = tuple(kind for kind, spec in ACTION_POLICIES.items() if spec.notification)
