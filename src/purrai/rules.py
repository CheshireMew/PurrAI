import hashlib
import re
import unicodedata
from collections import Counter
from urllib.parse import urlsplit


def normalize(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", text).casefold() if unicodedata.category(c) != "Cf"
    ).strip()


def content_digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def compact(text: str) -> str:
    return "".join(c for c in normalize(text) if c.isalnum())


def matches(text: str, value: str, mode: str) -> bool:
    if mode == "pinyin":
        from pypinyin import lazy_pinyin

        def tokens(value):
            return lazy_pinyin(compact(value))

        needle, haystack = tokens(value), tokens(text)
        return bool(needle) and any(
            haystack[i : i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1)
        )
    if mode == "compact":
        return bool(compact(value)) and compact(value) in compact(text)
    haystack, needle = normalize(text), normalize(value)
    return bool(needle) and (haystack == needle if mode == "exact" else needle in haystack)


def mentions(text: str) -> list[str]:
    # An address such as alice@example.com must not become a Telegram mention.
    normalized = normalize(text)
    found = [
        match.group(1)
        for match in re.finditer(r"(?<![a-z0-9_@])@([a-z0-9_]{1,32})(?![a-z0-9_])", normalized)
        if not re.match(r"\.[a-z]{2,}", normalized[match.end() :])
    ]
    return list(dict.fromkeys(found))


def similarity_signature(text: str) -> tuple[str, int]:
    original_length = len(compact(text))
    value = re.sub(r"(?:https?://|www\.|t\.me/)[^\s]+", " link ", normalize(text))
    value = re.sub(r"@[a-z0-9_]+", " user ", value)
    value = compact(re.sub(r"\d+", "number", value))[:2000]
    if len(value) < 8:
        return "", original_length
    grams = Counter(value[i : i + 3] for i in range(len(value) - 2))
    vector = [0] * 64
    for gram, count in grams.items():
        digest = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        for index in range(64):
            vector[index] += count if digest & (1 << index) else -count
    return f"{sum((1 << i) for i, value in enumerate(vector) if value >= 0):016x}", original_length


def is_similar(left: str, left_length: int, right: str, right_length: int, threshold: int) -> bool:
    if not left or not right or min(left_length, right_length) / max(left_length, right_length, 1) < 0.7:
        return False
    return 100 * (64 - (int(left, 16) ^ int(right, 16)).bit_count()) >= threshold * 64


def domain(value: str) -> str:
    value = normalize(value)
    host = (urlsplit(value if "://" in value else "https://" + value).hostname or "").rstrip(".")
    return host.removeprefix("www.").encode("idna").decode("ascii")


def domain_matches(host: str, configured: str) -> bool:
    target = domain(configured)
    return bool(target) and (host == target or host.endswith("." + target))


def message_content(message: dict) -> tuple[str, list[str]]:
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    urls = re.findall(r"(?:https?://|www\.)[^\s<>]+|(?:t\.me|telegram\.me)/[^\s<>]+", text, re.I)
    for entity in entities:
        if entity["type"] == "text_link":
            urls.append(entity.get("url", ""))
        elif entity["type"] == "url":
            encoded = text.encode("utf-16-le")
            start = entity["offset"] * 2
            urls.append(encoded[start : start + entity["length"] * 2].decode("utf-16-le", errors="ignore"))
    return text, [u.rstrip(".,，。!?！？)") for u in urls if u]


def assess(
    text: str,
    urls: list[str],
    rules: list[dict],
    settings: dict,
    newcomer: bool = False,
    contact: bool = False,
    repeats: int = 0,
    similar: int = 0,
    burst: int = 0,
    sender_name: str = "",
) -> dict:
    reasons, names = [], []
    active = [r for r in rules if r.get("enabled", True)]
    allowed = [r["value"] for r in active if r["kind"] == "allow_domain"]
    for rule in active:
        value = rule["value"]
        if rule["kind"] == "keyword" and value:
            mode, scope = rule.get("mode", "contains"), rule.get("scope", "message")
            body_hit, name_hit = matches(text, value, mode), matches(sender_name, value, mode)
            hit = (
                (name_hit and matches(text, rule.get("context_value", ""), mode))
                if scope == "name_and_message"
                else name_hit
                if scope == "name"
                else (name_hit or body_hit)
                if scope == "either"
                else body_hit
            )
            if hit:
                if mode == "pinyin":
                    label = "谐音关键词"
                elif mode == "compact":
                    label = "去除干扰关键词"
                else:
                    label = "关键词"
                if scope == "name_and_message":
                    names.append(f"昵称＋正文组合：{value} / {rule.get('context_value', '')}")
                    reasons.append(f"昵称＋正文组合：{value} / {rule.get('context_value', '')}")
                else:
                    if name_hit and scope in ("name", "either"):
                        names.append(f"昵称{label}：{value}")
                    if body_hit and scope in ("message", "either"):
                        reasons.append(f"{label}：{value}")
    allowed_mentions = {normalize(r["value"]).lstrip("@") for r in active if r["kind"] == "allow_mention"}
    blocked_mentions = {normalize(r["value"]).lstrip("@") for r in active if r["kind"] == "block_mention"}
    for username in mentions(sender_name):
        if username in blocked_mentions and username not in allowed_mentions:
            names.append(f"禁止用户名：@{username}")
    for username in mentions(text):
        if username in allowed_mentions:
            continue
        if username in blocked_mentions:
            reasons.append(f"禁止引流账号：@{username}")
        if newcomer and settings.get("newcomer_mentions"):
            reasons.append("新成员观察期提及未允许账号")
    for url in urls:
        host = domain(url)
        if any(domain_matches(host, item) for item in allowed):
            continue
        if host in ("t.me", "telegram.me"):
            username = (
                urlsplit(url if "://" in url else "https://" + url).path.strip("/").split("/")[0].casefold()
            )
            if username in allowed_mentions:
                continue
            if username in blocked_mentions:
                reasons.append(f"禁止引流账号：@{username}")
        if any(domain_matches(host, r["value"]) for r in active if r["kind"] == "block_domain"):
            reasons.append(f"禁止域名：{host}")
        path = urlsplit(url if "://" in url else "https://" + url).path
        if (
            settings.get("block_invites")
            and host in ("t.me", "telegram.me")
            and (path.startswith("/+") or path.startswith("/joinchat/"))
        ):
            reasons.append("群邀请链接")
        if newcomer and settings.get("newcomer_links"):
            reasons.append("新成员限制期发送链接")
    if newcomer and contact and settings.get("newcomer_contacts"):
        reasons.append("新成员限制期发送联系人")
    if settings.get("repeat_enabled") and repeats >= settings["repeat_count"]:
        reasons.append("重复刷屏")
    if settings.get("similar_enabled") and similar >= settings.get("similar_count", 4):
        reasons.append("近似内容刷屏")
    if settings.get("burst_enabled") and burst >= settings.get("burst_count", 8):
        reasons.append("短时间连续发送")
    return {"reasons": list(dict.fromkeys(reasons)), "name_reasons": list(dict.fromkeys(names))}


def evaluate(*args, **kwargs) -> list[str]:
    result = assess(*args, **kwargs)
    return result["reasons"] + result["name_reasons"]
