"""Parse Telegram targets without treating every t.me URL as a group."""

from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

HOSTS = {"t.me", "telegram.me", "telegram.dog"}
EMBEDDED = re.compile(r"(?<![a-z0-9_.])(?:(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/|tg://)[^\s<>\"'，。！？]+", re.I)
RESERVED = {"addstickers", "addemoji", "share", "proxy", "socks", "iv", "bg", "login", "addlist",
            "invoice", "giftcode", "boost", "premium", "m", "nft", "setlanguage", "confirmphone"}


@dataclass(frozen=True)
class TelegramLink:
    kind: str
    target: str
    post: str = ""

    @property
    def key(self):
        return f"{self.kind}:{self.target}" + (f"/{self.post}" if self.post else "")


def parse_link(value):
    value = "".join(char for char in unicodedata.normalize("NFKC", value) if unicodedata.category(char) != "Cf")
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        query = parse_qs(parsed.query)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.username or parsed.password:
            return None
        if parsed.scheme.lower() == "tg":
            if host == "join":
                path = "+" + query.get("invite", [""])[0]
            elif host == "resolve":
                path = query.get("domain", [""])[0]
                if query.get("post"):
                    path += "/" + query["post"][0]
            else:
                return None
        elif parsed.scheme.lower() in ("http", "https"):
            if host in HOSTS:
                path = unquote(parsed.path).strip("/")
            elif host.endswith(".t.me"):
                path = host[:-5] + parsed.path
            else:
                return None
        else:
            return None
    except (ValueError, UnicodeError):
        return None
    if path.startswith("joinchat/"):
        path = "+" + path[9:]
    if path.startswith("+"):
        token = re.match(r"\+([A-Za-z0-9_-]{8,128})(?=$|[/?#\s，。！？])", path)
        return TelegramLink("invite", token[1]) if token else None
    if path.startswith("s/"):
        path = path[2:]
    parts = path.split("/")
    if parts[0] == "c" and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return TelegramLink("private_post", "-100" + parts[1], parts[2])
    if parts[0].lower() in RESERVED:
        return None
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0]):
        if len(parts) > 1 and parts[1] and not parts[1].isdigit():
            return None
        return TelegramLink("public", parts[0].lower(), parts[1] if len(parts) > 1 else "")
    return None


def allowed_link(link, settings, rules=(), own_keys=(), group_id=None, username=""):
    if not link:
        return True
    if link.kind == "private_post" and link.target == str(group_id):
        return True
    if link.kind == "public" and link.target == username.lower().lstrip("@"):
        return True
    if link.key in own_keys or f"{link.kind}:{link.target}" in own_keys:
        return True
    for value in settings.get("telegram_allowed_links", []):
        allowed = parse_link(value)
        if allowed and (allowed == link or not allowed.post and (allowed.kind, allowed.target) == (link.kind, link.target)):
            return True
    return bool(link.kind == "public" and any(
        rule.get("enabled", True) and rule["kind"] == "allow_mention"
        and rule["value"].lower().lstrip("@") == link.target for rule in rules
    ))


def extract_links(values):
    links = []
    for value in values:
        # Handle a visible Telegram link nested in a redirect URL/query. This
        # inspects its actual text only; it never follows arbitrary websites.
        for _ in range(3):
            if link := parse_link(value.rstrip(".,，。!?！？);；]】}）")):
                links.append(link)
            for match in EMBEDDED.finditer(value):
                if link := parse_link(match.group().rstrip(".,，。!?！？);；]】}）")):
                    links.append(link)
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
    return list(dict.fromkeys(links))
