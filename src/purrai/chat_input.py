"""Chat exclusions use the same editable policy snapshot as moderation."""

from purrai.business_rules import BusinessEngine
from purrai.chat_safety import LINKS
from purrai.rules import assess, normalize


def solicitation(text, rules=None):
    hits = BusinessEngine(rules).run("chat_solicitation", text)
    return hits[0].reason if hits else ""


def advertising(text, *, direct=False, rules=None):
    hits = BusinessEngine(rules).run("chat_advertising", text, direct=direct)
    return hits[0].reason if hits else ""


def input_reasons(text, rules, settings, *, direct=False, urls=()):
    canonical = normalize(text)
    found = [match.group().rstrip(".,，。!?！？);；:：]】}）\"'") for match in LINKS.finditer(canonical)]
    reasons = assess(canonical, list(urls) + found, rules, settings)["reasons"]
    name = normalize(settings.get("chat_name", ""))
    body = canonical.removeprefix(name).lstrip(" ，,：:") if name else canonical
    reason = advertising(body, direct=direct, rules=rules)
    return list(dict.fromkeys(reasons + ([reason] if reason else [])))
