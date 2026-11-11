"""Remember advertised destinations globally, without blacklisting disposable senders."""

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from purrai.telegram_links import TelegramLink, allowed_link, extract_links, parse_link

PREFIX = "全局广告黑名单："
KINDS = {"account", "channel", "invite", "phone", "email", "contact", "domain", "url"}
SHORTENERS = {"t.co", "t.cn", "bit.ly", "tinyurl.com", "goo.gl", "cutt.ly", "is.gd", "linktr.ee"}


def normalize_target(value):
    value = "".join(c for c in unicodedata.normalize("NFKC", value).strip()
                    if unicodedata.category(c) != "Cf")
    kind, separator, content = value.partition(":")
    if separator and kind in KINDS:
        if kind == "url":
            try:
                parsed = urlsplit(content)
                if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
                    return "url:" + urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, ""))
            except ValueError:
                pass
            return ""
        if kind == "channel" and re.fullmatch(r"-100[0-9]+", content):
            return f"channel:{content}"
        if kind == "contact" and re.fullmatch(r"[1-9][0-9]+", content):
            return f"contact:{content}"
        if kind == "invite" and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", content):
            return f"invite:{content}"
        if kind in {"channel", "contact", "invite"}:
            return ""
        value = content
    else:
        kind = ""
    if link := parse_link(value):
        if link.kind == "public":
            return "account:" + link.target
        if link.kind == "private_post":
            return "channel:" + link.target
        if link.kind == "invite":
            if link.target.isdigit() and 7 <= len(link.target) <= 15:
                return "phone:" + link.target
            return "invite:" + link.target
    if value.startswith("@") or kind == "account":
        name = value.lstrip("@").lower()
        return "account:" + name if re.fullmatch(r"[a-z][a-z0-9_]{3,31}", name) else ""
    if re.fullmatch(r"-100[0-9]+", value):
        return "channel:" + value
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        return "email:" + value.casefold()
    if kind == "phone" or re.fullmatch(r"\+?[0-9][0-9 ()-]{6,24}[0-9]", value):
        digits = re.sub(r"\D", "", value)
        return "phone:" + digits if 7 <= len(digits) <= 15 else ""
    from purrai.text_matching import domain
    host = domain(value)
    if host == "wa.me":
        phone = urlsplit(value if "://" in value else "https://" + value).path.strip("/")
        return "phone:" + phone if phone.isdigit() and 7 <= len(phone) <= 15 else ""
    return "domain:" + host if "." in host and re.fullmatch(r"[a-z0-9.-]+", host) else ""


def advertised_targets(message, rules=(), *, settings=None, urls=None, evidence=(), exclude_sender=False):
    from purrai.advertising_patterns import authored_text
    from purrai.text_matching import domain, domain_matches, mentions, message_content
    from purrai.telegram_sources import channel_sources

    text, message_urls = message_content(message)
    settings = settings or {}
    active = [rule for rule in rules if rule.get("enabled", True)]
    allowed_domains = [rule["value"] for rule in active if rule["kind"] == "allow_domain"]
    allowed_accounts = {rule["value"].lower().lstrip("@") for rule in active
                        if rule["kind"] == "allow_mention"}
    sender = message.get("from", {})
    sender_name = sender.get("username", "").lower()
    group_id = message.get("chat", {}).get("id")
    found = set()

    def add(value):
        key = normalize_target(value)
        if not key:
            return
        kind, _, target = key.partition(":")
        if kind == "account" and (exclude_sender and target == sender_name or target in allowed_accounts):
            return
        if kind == "account" and allowed_link(TelegramLink("public", target), settings, rules,
                                               group_id=group_id, username=message.get("chat", {}).get("username", "")):
            return
        if kind == "channel" and target == str(group_id):
            return
        if kind == "contact" and exclude_sender and target == str(sender.get("id")):
            return
        if kind == "domain" and any(domain_matches(target, item) for item in allowed_domains):
            return
        found.add(key)

    authored = authored_text(text, rules)
    for url in message_urls if urls is None else urls:
        authored = authored.replace(url, "")
    for username in mentions(authored):
        add("@" + username)
    for url in message_urls if urls is None else urls:
        host = domain(url)
        if any(domain_matches(host, item) for item in allowed_domains):
            continue
        links = extract_links([url])
        if links:
            for link in links:
                if allowed_link(link, settings, rules, group_id=group_id,
                                username=message.get("chat", {}).get("username", "")):
                    continue
                if link.kind == "public":
                    add("@" + link.target)
                elif link.kind == "private_post":
                    add("channel:" + link.target)
                elif link.kind == "invite":
                    add(("phone:" if link.target.isdigit() and 7 <= len(link.target) <= 15 else "invite:") + link.target)
        elif host:
            add("url:" + url if host in SHORTENERS else url if host == "wa.me" else "domain:" + host)
    for source in channel_sources(message):
        post = str(source.get("post") or "")
        if allowed_link(TelegramLink("private_post", str(source["id"]), post), settings, rules,
                        group_id=group_id) or (source.get("username") and allowed_link(
                        TelegramLink("public", source["username"].lower(), post), settings, rules,
                        group_id=group_id)):
            continue
        add("channel:" + str(source["id"]))
        if source.get("username"):
            add("@" + source["username"])
    for proof in evidence:
        if proof.get("link") and allowed_link(TelegramLink(**proof["link"]), settings, rules, group_id=group_id):
            continue
        if proof.get("type") in ("group", "supergroup", "channel") and proof.get("id"):
            add("channel:" + str(proof["id"]))
    contact = message.get("contact") or {}
    if contact.get("phone_number"):
        add("phone:" + contact["phone_number"])
    if contact.get("user_id"):
        add("contact:" + str(contact["user_id"]))
    for entity in message.get("entities") or message.get("caption_entities") or []:
        if entity.get("type") == "text_mention" and entity.get("user", {}).get("id"):
            add("contact:" + str(entity["user"]["id"]))
    for email in re.findall(r"(?<![\w@])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w@])", authored):
        add(email)
    for phone in re.findall(r"(?<![\w@])\+[0-9][0-9 ()-]{6,24}[0-9](?!\w)", authored):
        add(phone)
    for phone in re.findall(r"(?:电话|手机|联系电话|tel|phone|whatsapp)[ :：]*([+0-9][0-9 ()-]{6,24}[0-9])", authored, re.I):
        add("phone:" + phone)
    return sorted(found)


def blacklist_reasons(message, rules, *, settings=None, urls=None, evidence=()):
    from purrai.advertising_patterns import discussion

    blocked = {normalize_target(rule["value"]) for rule in rules
               if rule.get("enabled", True) and rule["kind"] == "block_target"}
    if not blocked:
        return []
    hits = blocked.intersection(advertised_targets(message, rules, settings=settings, urls=urls, evidence=evidence))
    text = message.get("text") or message.get("caption") or ""
    prefix = "举报中引用广告黑名单，仅删除：" if discussion(text, rules) else PREFIX
    return [prefix + target for target in sorted(hits)]


async def independent_target_rules(session, rules, group_id, message_id):
    """A message's own learned targets cannot justify its stale queued penalty."""
    from sqlalchemy import or_, select
    from purrai.models import PromotionSource

    own = set(await session.scalars(select(PromotionSource.rule_id).where(
        PromotionSource.group_id == group_id, PromotionSource.message_id == message_id,
        PromotionSource.withdrawn_at == 0)))
    if not own:
        return rules
    elsewhere = set(await session.scalars(select(PromotionSource.rule_id).where(
        PromotionSource.rule_id.in_(own), PromotionSource.withdrawn_at == 0,
        or_(PromotionSource.group_id != group_id, PromotionSource.message_id != message_id))))
    from purrai.business_rules import with_rules
    return with_rules(rules, [rule for rule in rules if rule.get("origin") != "learned" or rule["id"] not in own - elsewhere])


async def remember_targets(session, group, violation, evidence):
    from purrai.action_queue import enqueue
    from purrai.advertising_patterns import discussion
    from purrai.models import PromotionSource, SharedPolicy, now, uid
    from purrai.advertising_constants import CONTACT_REASONS
    from purrai.shared_policy import effective_rules, policy

    message = (evidence or {}).get("message")
    rules = await effective_rules(session, group.id)
    if not message or discussion(message.get("text") or message.get("caption") or "", rules):
        return []
    independent_ad = violation.rule_mute or any(
        reason in CONTACT_REASONS or reason.startswith(("明确广告引流：", "外群链接引流：", "外部频道广告："))
        for reason in violation.reasons
    )
    if not independent_ad:
        return []
    current = await policy(session)
    if current not in session:
        current = SharedPolicy(id=1, settings=current.settings, rules=current.rules, business_rules=current.business_rules)
        session.add(current)
    existing = {normalize_target(rule["value"]): rule for rule in current.rules if rule["kind"] == "block_target"}
    observed = advertised_targets(message, rules,
               settings=group.settings, evidence=evidence.get("links", []), exclude_sender=True)
    if not observed:
        return []
    targets = [target for target in observed if target not in existing]
    new_rules = [{"id": uid(), "kind": "block_target", "value": target, "mode": "exact",
                  "scope": "message", "context_value": "", "penalty": "delete", "enabled": True,
                  "origin": "learned", "learned_at": now()}
                 for target in targets]
    if targets:
        current.rules = [*current.rules, *new_rules]
    indexed = {**existing, **{rule["value"]: rule for rule in new_rules}}
    for target in observed:
        rule = indexed[target]
        if not await session.get(PromotionSource, (rule["id"], group.id, violation.message_id)):
            session.add(PromotionSource(rule_id=rule["id"], group_id=group.id, message_id=violation.message_id,
                                       violation_id=getattr(violation, "id", "")))
    if not targets:
        return []
    action = await enqueue(session, "rule", f"learn-ad-targets:{group.id}:{violation.message_id}",
                           group.id, payload={"result": {"targets": targets, "source_group_id": group.id,
                           "source_message_id": violation.message_id, "reasons": violation.reasons}},
                           reason="广告推广目标加入全局黑名单")
    previous = action.payload.get("result", {}).get("targets", [])
    action.payload = {"result": {"targets": sorted(set(previous) | set(targets)),
                      "source_group_id": group.id, "source_message_id": violation.message_id,
                      "reasons": violation.reasons}}
    action.state = "succeeded"
    return targets
