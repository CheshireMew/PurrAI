"""Pure normalization, URL parsing and content fingerprints shared by rule evaluators."""

import hashlib
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from urllib.parse import urlsplit


_NUMBER_CHAR = r"(?:\d(?:[\ufe0e\ufe0f]?\u20e3)?|(?ai:[ilozsb])|[〇○οΟоО🔟❶-❿➀-➉➊-➓⓵-⓾⓿])"
_NUMBER_LIKE = re.compile(
    rf"(?<![a-zA-Z0-9_@])(?:[丨│]|\|(?=[0oO〇○οΟоО]))?{_NUMBER_CHAR}+(?:[.,]{_NUMBER_CHAR}+)*(?![a-zA-Z0-9_])"
)
_NUMBER_TRANSLATION = str.maketrans({
    "o": "0", "〇": "0", "○": "0", "ο": "0", "о": "0",
    "i": "1", "l": "1", "丨": "1", "│": "1", "|": "1",
    "z": "2", "s": "5", "b": "8", "🔟": "10",
    "\ufe0e": None, "\ufe0f": None, "\u20e3": None,
})


def normalize(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", text).casefold() if unicodedata.category(c) != "Cf"
    ).strip()


def _number_reading(match: re.Match) -> str:
    fragment = match[0]
    # A run of Latin letters alone is too ambiguous, as are short codes like O2.
    if not any(char.isnumeric() or char in "丨│|○🔟" for char in fragment):
        return fragment
    reading = fragment.lower().translate(_NUMBER_TRANSLATION)
    if fragment.isascii() and sum(char.isdecimal() for char in reading) < 3:
        return fragment
    return "".join(str(int(unicodedata.numeric(char))) if char.isnumeric() else char for char in reading)


def normalize_number_variants(value: str) -> str:
    """Convert number-like spans in normalized text prepared for sample matching."""
    return _NUMBER_LIKE.sub(_number_reading, value)


def number_readings(value: str) -> dict[str, str]:
    """Offer possible readings for user text outside visible URLs."""
    for url in message_content({"text": value})[1]:
        value = value.replace(url, " ")
    return {match[0]: reading for match in _NUMBER_LIKE.finditer(value)
            if (reading := _number_reading(match)) != match[0]}


def content_digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def compact(text: str) -> str:
    return "".join(c for c in normalize(text) if c.isalnum())


@lru_cache(maxsize=2048)
def matching_text(value: str, mode: str):
    if mode in ("pinyin", "pinyin_literal"):
        from pypinyin import lazy_pinyin

        han = compact(value)
        if mode == "pinyin":
            # Also examine a view without letters/digits inserted between Han.
            # The literal view preserves intentionally configured mixed terms.
            han = re.sub(r"(?<=[\u4e00-\u9fff])[a-z0-9]+(?=[\u4e00-\u9fff])", "", han)
        tokens = lazy_pinyin(han, errors=lambda value: list(value))
        return "\0" + "\0".join(tokens) + "\0" if tokens else ""
    return compact(value) if mode == "compact" else normalize(value)


def matches(text: str, value: str, mode: str) -> bool:
    if mode == "pinyin":
        needle = matching_text(value, "pinyin_literal")
        return bool(needle) and any(needle in matching_text(text, view) for view in ("pinyin_literal", "pinyin"))
    if mode == "compact":
        needle = matching_text(value, mode)
        return bool(needle) and needle in matching_text(text, mode)
    haystack, needle = matching_text(text, mode), matching_text(value, mode)
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


def parsed_url(value: str):
    value = normalize(value)
    try:
        return urlsplit(value if "://" in value else "https://" + value)
    except (ValueError, UnicodeError):
        return None


def domain(value: str) -> str:
    parsed = parsed_url(value)
    if parsed is None:
        return ""
    try:
        host = (parsed.hostname or "").rstrip(".")
        return host.removeprefix("www.").encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        # Text and Telegram entities can contain incomplete or invalid URLs.
        # They must not abort the other moderation rules for this message.
        return ""


def domain_matches(host: str, configured: str) -> bool:
    target = domain(configured)
    return bool(target) and (host == target or host.endswith("." + target))


def message_content(message: dict) -> tuple[str, list[str]]:
    from purrai.message_parts import content_parts
    parts = content_parts(message)
    urls = []
    # Keep the original text for UTF-16 entity offsets and case-sensitive paths.
    for part in parts:
        text = part.text
        visible = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
        urls.extend(re.findall(r"(?:https?://|tg://|www\.)[^\s<>]+|(?<![A-Za-z0-9_.])(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>]+", visible, re.I))
        for entity in part.entities:
            if entity.get("type") == "text_link":
                urls.append(entity.get("url", ""))
            elif entity.get("type") == "url":
                encoded = text.encode("utf-16-le")
                start = entity["offset"] * 2
                urls.append(encoded[start : start + entity["length"] * 2].decode("utf-16-le", errors="ignore"))
    from purrai.message_buttons import inline_buttons
    urls.extend(button["url"] for button in inline_buttons(message) if isinstance(button["url"], str))
    return "\n".join(part.text for part in parts if part.text), list(dict.fromkeys(u.rstrip(".,，。!?！？)") for u in urls if u))
