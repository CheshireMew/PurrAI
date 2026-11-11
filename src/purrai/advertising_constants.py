"""Stable moderation reason identifiers, independent of evaluation and storage."""

LEGACY_CONTACT_REASON = "分享联系人，删除消息并踢出群聊"
CONTACT_REASON = "分享联系人，删除消息并永久封禁"
CONTACT_REASONS = (LEGACY_CONTACT_REASON, CONTACT_REASON)
AD_PREFIX = "明确广告引流："
GROUP_PREFIX = "外群链接引流："
CHANNEL_PREFIX = "外部频道广告："
BLACKLIST_PREFIX = "全局广告黑名单："


def is_ban_reason(reason):
    return reason.startswith((AD_PREFIX, GROUP_PREFIX, CHANNEL_PREFIX, BLACKLIST_PREFIX))
