import re

from purrai.wallet_addresses import contains_wallet_address
from purrai.business_rules import BusinessEngine, BusinessReason, with_rules
from purrai.reason_policy import unique_reasons
from purrai.text_matching import (
    normalize as normalize,
    content_digest as content_digest,
    compact as compact,
    matching_text as matching_text,
    matches as matches,
    mentions as mentions,
    similarity_signature as similarity_signature,
    is_similar as is_similar,
    parsed_url as parsed_url,
    domain as domain,
    domain_matches as domain_matches,
    message_content as message_content,
)

from purrai.advertising_constants import (
    BUTTON_REASON,
    CONTACT_REASON as CONTACT_REASON,
    CONTACT_REASONS as CONTACT_REASONS,
    LEGACY_CONTACT_REASON as LEGACY_CONTACT_REASON,
)


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
    *,
    forwarded: bool = False,
    mentioned_users: list[dict] | None = None,
    source_titles: list[str] | None = None,
    inline_buttons: bool = False,
    message_emoji_packs: list[str] | tuple = (),
    sender_bio: str = "",
) -> dict:
    reasons, names, mute_reasons, hit_ids = [], [], [], []
    engine = BusinessEngine(rules)
    body_text = engine.view("s_auth", text)
    body_discussion = engine.condition("communication_context", text)
    ban_reasons = []

    def record_rule(reason, penalty, *, profile=False):
        action = {"permanent_mute": "mute", "ban": "ban"}.get(penalty, "delete")
        reason = BusinessReason(reason, action)
        if profile and action == "mute":
            names.append(reason)
        else:
            reasons.append(reason)
            if action == "mute":
                mute_reasons.append(reason)
            elif action == "ban":
                ban_reasons.append(reason)
    if inline_buttons and settings.get("inline_buttons_ban", False):
        reasons.append(BusinessReason(BUTTON_REASON, "ban"))
        ban_reasons.append(reasons[-1])
    if settings.get("advertising_ban", True):
        hits = [(hit, True) for hit in engine.run("profile", sender_name)]
        hits += [(hit, False) for stage in ("short", "advertising")
                  for hit in engine.run(stage, normalize(text), source_titles=[normalize(title) for title in source_titles or []], urls=urls)]
        for hit, profile in hits:
            hit_ids.append(hit.id)
            if profile and hit.action == "mute":
                names.append(hit.reason)
            else:
                reasons.append(hit.reason)
                if hit.action == "mute":
                    mute_reasons.append(hit.reason)
                elif hit.action == "ban":
                    ban_reasons.append(hit.reason)
    active = with_rules(rules, [r for r in rules if r.get("enabled", True)])
    emoji_hit = False
    for rule in active:
        if rule["kind"] == "block_emoji_pack" and rule["value"].lower() in message_emoji_packs:
            emoji_hit = True
            reason = f"消息表情包黑名单：{rule['value']}"
            record_rule(reason, rule.get("penalty", "delete"))
            if rule.get("id"):
                hit_ids.append(rule["id"])
    from purrai.promotion_targets import blacklist_reasons
    target_reasons = blacklist_reasons({"text": text}, active, settings=settings, urls=urls)
    reasons.extend(target_reasons)
    mute_reasons.extend(reason for reason in target_reasons if getattr(reason, "action", "") == "mute")
    hit_ids.extend(reason.rule_id for reason in target_reasons if reason.rule_id)
    ban_reasons.extend(reason for reason in target_reasons if getattr(reason, "action", "") == "ban")
    allowed = [r["value"] for r in active if r["kind"] == "allow_domain"]
    for rule in active:
        value = rule["value"]
        if rule["kind"] == "keyword" and value and rule.get("penalty") != "ai_confirmed_mute":
            mode, scope = rule.get("mode", "contains"), rule.get("scope", "message")
            body_hit = scope in ("message", "either") and not body_discussion and matches(body_text, value, mode)
            name_hit = scope != "message" and matches(sender_name, value, mode)
            hit = (
                (name_hit and not body_discussion and matches(body_text, rule.get("context_value", ""), mode))
                if scope == "name_and_message"
                else name_hit
                if scope == "name"
                else (name_hit or body_hit)
                if scope == "either"
                else body_hit
            )
            if hit:
                if rule.get("id"):
                    hit_ids.append(rule["id"])
                if mode == "pinyin":
                    label = "谐音关键词"
                elif mode == "compact":
                    label = "去除干扰关键词"
                else:
                    label = "关键词"
                if scope == "name_and_message":
                    record_rule(f"昵称＋正文组合：{value} / {rule.get('context_value', '')}",
                                rule.get("name_penalty", "permanent_mute"), profile=True)
                else:
                    if name_hit and scope in ("name", "either"):
                        record_rule(f"昵称{label}：{value}", rule.get("name_penalty", "permanent_mute"), profile=True)
                    if body_hit and scope in ("message", "either"):
                        record_rule(f"{label}：{value}", rule.get("penalty", "delete"))
    allowed_mentions = {normalize(r["value"]).lstrip("@") for r in active if r["kind"] == "allow_mention"}
    blocked_mentions = {normalize(r["value"]).lstrip("@") for r in active if r["kind"] == "block_mention"}
    for username in mentions(sender_name):
        if username in blocked_mentions and username not in allowed_mentions:
            for rule in active:
                if rule["kind"] == "block_mention" and normalize(rule["value"]).lstrip("@") == username:
                    record_rule(f"禁止用户名：@{username}", rule.get("name_penalty", "permanent_mute"), profile=True)
                    if rule.get("id"):
                        hit_ids.append(rule["id"])
    for username in mentions(text):
        if username in allowed_mentions:
            continue
        if username in blocked_mentions:
            reasons.append(f"禁止引流账号：@{username}")
            hit_ids.extend(r["id"] for r in active if r.get("id") and r["kind"] == "block_mention"
                           and normalize(r["value"]).lstrip("@") == username)
        if newcomer and settings.get("newcomer_mentions"):
            reasons.append("新成员观察期提及未允许账号")
    if newcomer and settings.get("newcomer_mentions"):
        if re.search(r"(?<![a-z0-9_@])@-\d{1,20}(?![a-z0-9_])", normalize(text)) or any(
            normalize(user.get("username", "")) not in allowed_mentions for user in mentioned_users or []
        ):
            reasons.append("新成员观察期提及未允许账号或群组")
    for url in urls:
        host = domain(url)
        parsed = parsed_url(url)
        path = parsed.path if parsed else ""
        if any(domain_matches(host, item) for item in allowed):
            continue
        if host in ("t.me", "telegram.me"):
            username = path.strip("/").split("/")[0].casefold()
            if username in allowed_mentions:
                continue
            if username in blocked_mentions:
                reasons.append(f"禁止引流账号：@{username}")
                hit_ids.extend(r["id"] for r in active if r.get("id") and r["kind"] == "block_mention"
                               and normalize(r["value"]).lstrip("@") == username)
        blocked_domains = [r for r in active if r["kind"] == "block_domain" and domain_matches(host, r["value"])]
        if blocked_domains:
            reasons.append(f"禁止域名：{host}")
            hit_ids.extend(r["id"] for r in blocked_domains if r.get("id"))
        if (
            settings.get("block_invites")
            and host in ("t.me", "telegram.me")
            and (path.startswith("/+") or path.startswith("/joinchat/"))
        ):
            reasons.append("群邀请链接")
        if newcomer and settings.get("newcomer_links"):
            reasons.append("新成员限制期发送链接")
    if contact:
        reasons.append(BusinessReason(CONTACT_REASON, "ban", contact=True))
    if newcomer and forwarded and settings.get("newcomer_forwards", True):
        reasons.append("新成员观察期转发用户或频道消息")
    if settings.get("newcomer_wallets", True) and any(
        contains_wallet_address(value) for value in [text, *urls]
    ):
        reasons.append("发送加密货币或代币合约地址")
    reasons.extend(activity_reasons(settings, repeats, similar, burst))
    bio_reasons = []
    if sender_bio and settings.get("bio_advertising_enabled", True):
        bio_reasons, bio_ids = assess_biography(sender_bio, rules, settings)
        hit_ids.extend(bio_ids)
        names.extend(reason for reason in bio_reasons if reason.action == "mute")
        reasons.extend(reason for reason in bio_reasons if reason.action != "mute")
        ban_reasons.extend(reason for reason in bio_reasons if reason.action == "ban")
    return {"reasons": unique_reasons(reasons), "name_reasons": unique_reasons(names), "bio_reasons": bio_reasons,
            "mute_reasons": list(dict.fromkeys(mute_reasons)), "ban_reasons": ban_reasons,
            "rule_ids": list(dict.fromkeys(hit_ids)), "message_emoji_hit": emoji_hit}


def assess_biography(value, rules, settings):
    # Only biography keywords participate. Domain and target policies apply to
    # both sources. Evaluate the biography as text, without another biography.
    selected = with_rules(rules, [{**rule, "scope": "message"} if rule["kind"] == "keyword" else rule
                                  for rule in rules if rule["kind"] != "keyword" or rule.get("scope") == "bio"])
    text, urls = message_content({"text": value})
    result = assess(text, urls, selected, {**settings, "newcomer_wallets": False})
    reasons = []
    for reason in result["reasons"] + result["name_reasons"]:
        reasons.append(BusinessReason("用户简介：" + str(reason), getattr(reason, "action", "") or "delete",
                       getattr(reason, "rule_id", ""), getattr(reason, "advertiser_cooldown", False)))
    return reasons, result["rule_ids"]


def ai_keyword_rules(text, rules, sender_name="", sender_bio=""):
    """Candidates only: a keyword never supplies the model's advertising verdict."""
    return [rule for rule in rules if rule.get("enabled", True) and rule["kind"] == "keyword"
            and rule.get("penalty") == "ai_confirmed_mute"
            and ((rule.get("scope", "message") in ("message", "either")
                  and matches(text, rule["value"], rule.get("mode", "contains")))
                 or (rule.get("scope") in ("name", "either")
                     and matches(sender_name, rule["value"], rule.get("mode", "contains")))
                 or (rule.get("scope") == "bio" and matches(sender_bio, rule["value"], rule.get("mode", "contains"))))]


def ai_keyword_hints(text, rules, sender_name="", sender_bio=""):
    """Explain only matches belonging to the target sender, using editable data."""
    hints = []
    for rule in ai_keyword_rules(text, rules, sender_name, sender_bio):
        fields = []
        for field, value in (("name", sender_name), ("message", text), ("bio", sender_bio)):
            if (rule.get("scope", "message") == field or field != "bio" and rule.get("scope") == "either") and matches(
                value, rule["value"], rule.get("mode", "contains")
            ):
                fields.append(field)
        hints.append({"rule_id": rule.get("id", ""), "keyword": rule["value"],
                      "meaning": rule.get("meaning", ""), "matched_fields": fields})
    return hints


def activity_reasons(settings, repeats, similar, burst):
    reasons = []
    if settings.get("repeat_enabled") and repeats >= settings["repeat_count"]:
        reasons.append("重复刷屏")
    if settings.get("similar_enabled") and similar >= settings.get("similar_count", 4):
        reasons.append("近似内容刷屏")
    if settings.get("burst_enabled") and burst >= settings.get("burst_count", 8):
        reasons.append("短时间连续发送")
    return reasons


def evaluate(*args, **kwargs) -> list[str]:
    result = assess(*args, **kwargs)
    return result["reasons"] + result["name_reasons"]
