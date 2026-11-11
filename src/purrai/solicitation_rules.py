"""Compatibility entry points for editable business-policy stages."""

from purrai.business_rules import BusinessEngine

PREFIX = "明确广告引流："
SHORT_PREFIX = "业务招揽短语，仅删除："


def normalized(text, rules=None):
    return BusinessEngine(rules).view("s_norm", text)


def compact(text, rules=None):
    return "".join(char for char in normalized(text, rules) if char.isalnum())


def authored(text, rules=None):
    return BusinessEngine(rules).view("s_auth", text)


def communication_context(text, rules=None):
    return BusinessEngine(rules).condition("communication_context", text)


def solicitation_reasons(text, rules=None):
    return [hit.reason for hit in BusinessEngine(rules).run("solicitation", text)]


def short_solicitation_reasons(text, rules=None):
    return [hit.reason for hit in BusinessEngine(rules).run("short", text)]


def profile_solicitation_reasons(name, rules=None):
    return [hit.reason for hit in BusinessEngine(rules).run("profile", name)]


def contact_should_block(message, settings, rules=None):
    card = message.get("contact")
    if not card:
        return False
    if not settings.get("contact_ads_only", False):
        return True
    name = " ".join(str(card.get(key, "")) for key in ("first_name", "last_name"))
    return bool(profile_solicitation_reasons(name, rules)
                or solicitation_reasons(message.get("text") or message.get("caption") or "", rules))
