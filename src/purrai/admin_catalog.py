"""The administration contract consumed by both Telegram and the web console."""

from purrai.admin_labels import BOOLEANS, CHAT_FIELDS, FAILURE_ACTIONS, KINDS, MODES, NUMBERS, OPERATIONS, SCOPES, STATE
from purrai.schemas import GroupSettings
from purrai.verification_state import ACTIVE


def admin_catalog():
    return {
        "settings": {
            "defaults": GroupSettings().model_dump(),
            "fields": GroupSettings.model_json_schema()["properties"],
            "numbers": NUMBERS,
            "booleans": BOOLEANS,
            "labels": {**{key: field.title or key for key, field in GroupSettings.model_fields.items()},
                       **BOOLEANS, **NUMBERS, **CHAT_FIELDS,
                       "verification_failure_action": "验证失败处理", "chat_enabled": "群聊天",
                       "chat_proactive": "主动参与讨论", "chat_stickers": "聊天贴纸",
                       "chat_antifraud": "防诈贴纸提醒", "chat_command_learning": "自动识别口令",
                       "emoji_status_blocked_packs": "状态表情包黑名单", "telegram_allowed_links": "允许的 Telegram 链接",
                       "newcomer_contacts": "旧版联系人开关（已不使用）", "warning_window": "旧版警告统计窗口（已不使用）",
                       "warning_limit": "旧版警告次数（已不使用）"},
        },
        "states": STATE,
        "operations": OPERATIONS,
        "failure_actions": FAILURE_ACTIONS,
        "active_verifications": list(ACTIVE),
        "rules": {"kinds": KINDS, "modes": MODES, "scopes": SCOPES},
    }
