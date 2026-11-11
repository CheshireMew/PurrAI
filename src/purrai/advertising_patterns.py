"""Compatibility entry points; all business conditions come from editable data."""

from purrai.business_rules import BusinessEngine
from purrai.advertising_constants import (
    AD_PREFIX as AD_PREFIX, GROUP_PREFIX as GROUP_PREFIX, is_ban_reason as is_ban_reason,
)


def compact(value):
    return "".join(char for char in value if char.isalnum())


def authored_text(text, rules=None):
    return BusinessEngine(rules).view("a_auth", text)


def contact_audience(text, rules=None):
    return BusinessEngine(rules).condition("contact_audience", text)


def invites_audience(text, rules=None):
    return BusinessEngine(rules).condition("invites_audience", text)


def discussion(text, rules=None):
    return BusinessEngine(rules).condition("discussion", text)


def advertising_reasons(text, source_titles=(), rules=None):
    return [hit.reason for hit in BusinessEngine(rules).run("advertising", text, source_titles=source_titles)]


def source_titles(message):
    found = []
    for item in (message, message.get("reply_to_message", {}), message.get("external_reply", {})):
        origin = item.get("forward_origin") or item.get("origin") or {}
        for chat in (origin.get("chat", {}), origin.get("sender_chat", {}),
                     item.get("forward_from_chat", {}), item.get("sender_chat", {})):
            if chat.get("type") in ("channel", "supergroup", "group") and chat.get("title"):
                found.append(chat["title"])
    return found


def profile_advertising_reasons(name, rules=None):
    from purrai.solicitation_rules import profile_solicitation_reasons
    return profile_solicitation_reasons(name, rules)
