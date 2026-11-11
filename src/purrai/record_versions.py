"""Content versions for editable records, independent of ORM and transports."""

import hashlib
import json

from purrai.errors import ManagementError

RULE_FIELDS = ("id", "group_id", "kind", "value", "mode", "scope", "context_value", "penalty", "enabled", "inherited", "inherited_from")
WHITE_FIELDS = ("group_id", "user_id", "verification", "moderation", "note")
AUTO_REPLY_FIELDS = ("id", "group_id", "keyword", "reply", "mode", "enabled", "priority", "cooldown_seconds")


def record_version(value):
    if value is None:
        return "missing"
    fields = AUTO_REPLY_FIELDS if "reply" in value else RULE_FIELDS if "kind" in value else WHITE_FIELDS
    encoded = json.dumps({key: value.get(key) for key in fields}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def versioned(value):
    return {**value, "version": record_version(value)}


def check_version(value, expected):
    if expected is None:
        raise ManagementError(428, "请刷新记录后再提交修改")
    if expected != record_version(value):
        raise ManagementError(409, "记录已被修改，请刷新后核对；你的草稿已保留")
