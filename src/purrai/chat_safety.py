"""Deterministic checks on generated text; never create member penalties."""

import re
from urllib.parse import urlsplit

from purrai.rules import assess, domain, domain_matches, mentions, normalize

# Include bare domains and email addresses: Telegram can linkify them even when
# the model omits https://. Normalize full-width and zero-width characters first.
LINKS = re.compile(
    r"[a-z][a-z0-9+.-]*://[^\s<>]+|(?:mailto|tg|data|javascript):[^\s<>]+"
    r"|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/[^\s<>]*)?"
    r"|(?<![\w@.-])(?:[\w.+-]+@)?(?:[^\W_][\w-]*\.)+"
    r"(?:[^\W\d_][\w-]+)(?::\d+)?(?:/[^\s<>]*)?",
    re.I,
)
MANAGEMENT = re.compile(
    r"(?:^|\s)/(?:ban|mute|unban|unmute|release|reissue)(?:@[a-z0-9_]+)?(?=\s|$)"
    r"|(?:^|[\n。！？])\s*[【\[]?(?:系统|管理员|群主|官方)(?:公告|通知|指令)[】\]]?\s*[:：]"
    r"|(?:我(?:已经?|刚刚)|已成功)(?:将|把)[^。！？\n]{0,40}(?:禁言|封禁|踢出|移出)"
    r"|(?:我(?:已经?|刚刚)|已成功)(?:永久)?(?:禁言|封禁|踢出|移出|解除了?禁言|解除了?封禁)"
    r"|\b(?:I(?:'ve| have)|successfully)\s+(?:banned|muted|kicked|unbanned|unmuted)\b",
    re.I,
)


def output_reasons(text: str | None, rules: list[dict], settings: dict, bot_username="") -> list[str]:
    if not text:
        return []
    canonical = normalize(text).replace("[.]", ".").replace("(.)", ".")
    urls = [match.group().rstrip(".,，。!?！？);；:：]】}）\"'") for match in LINKS.finditer(canonical)]
    active = [rule for rule in rules if rule.get("enabled", True)]
    allowed_domains = [rule["value"] for rule in active if rule["kind"] == "allow_domain"]
    allowed_mentions = {
        normalize(rule["value"]).lstrip("@") for rule in active if rule["kind"] == "allow_mention"
    }
    if bot_username:
        allowed_mentions.add(normalize(bot_username).lstrip("@"))
    try:
        # Only message/either scopes apply. A user's name is not the AI's output.
        reasons = assess(text, urls, active, settings)["reasons"]
        for url in urls:
            parsed = urlsplit(url if "://" in url else "https://" + url)
            host = domain(url)
            if (
                parsed.scheme not in ("http", "https")
                or not host
                or parsed.username is not None
                or parsed.password is not None
            ):
                reasons.append("不允许的链接格式")
                continue
            if any(domain_matches(host, value) for value in allowed_domains):
                continue
            if host in ("t.me", "telegram.me") and parsed.path.strip("/") in allowed_mentions:
                continue
            reasons.append("未经允许的链接")
    except (ValueError, UnicodeError):
        # Malformed model-generated URLs must not bypass the check or cause retries.
        return ["无法安全识别的链接"]
    if any(username not in allowed_mentions for username in mentions(canonical)):
        reasons.append("未经允许的引流账号")
    if MANAGEMENT.search(canonical):
        reasons.append("管理指令或冒充管理操作")
    return list(dict.fromkeys(reasons))
