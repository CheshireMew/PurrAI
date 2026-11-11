import re

from purrai.wallet_addresses import contains_wallet_address
from purrai.business_rules import BusinessEngine
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
) -> dict:
    reasons, names, mute_reasons, hit_ids = [], [], [], []
    engine = BusinessEngine(rules)
    body_text = engine.view("s_auth", text)
    body_discussion = engine.condition("communication_context", text)
    ban_reasons = []
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
    active = [r for r in rules if r.get("enabled", True)]
    from purrai.promotion_targets import blacklist_reasons
    target_reasons = blacklist_reasons({"text": text}, active, settings=settings, urls=urls)
    reasons.extend(target_reasons)
    hit_ids.extend(rule["id"] for rule in active if rule.get("id") and rule["kind"] == "block_target"
                   and any(reason.endswith(rule["value"]) for reason in target_reasons))
    ban_reasons.extend(reason for reason in target_reasons if reason.startswith("全局广告黑名单："))
    allowed = [r["value"] for r in active if r["kind"] == "allow_domain"]
    for rule in active:
        value = rule["value"]
        if rule["kind"] == "keyword" and value:
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
                    names.append(f"昵称＋正文组合：{value} / {rule.get('context_value', '')}")
                    reasons.append(f"昵称＋正文组合：{value} / {rule.get('context_value', '')}")
                else:
                    if name_hit and scope in ("name", "either"):
                        names.append(f"昵称{label}：{value}")
                    if body_hit and scope in ("message", "either"):
                        reasons.append(f"{label}：{value}")
                        if rule.get("penalty") == "permanent_mute":
                            mute_reasons.append(f"{label}：{value}")
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
        if any(domain_matches(host, r["value"]) for r in active if r["kind"] == "block_domain"):
            reasons.append(f"禁止域名：{host}")
        if (
            settings.get("block_invites")
            and host in ("t.me", "telegram.me")
            and (path.startswith("/+") or path.startswith("/joinchat/"))
        ):
            reasons.append("群邀请链接")
        if newcomer and settings.get("newcomer_links"):
            reasons.append("新成员限制期发送链接")
    if contact:
        reasons.append(CONTACT_REASON)
    if newcomer and forwarded and settings.get("newcomer_forwards", True):
        reasons.append("新成员观察期转发用户或频道消息")
    if settings.get("newcomer_wallets", True) and any(
        contains_wallet_address(value) for value in [text, *urls]
    ):
        reasons.append("发送加密货币或代币合约地址")
    reasons.extend(activity_reasons(settings, repeats, similar, burst))
    for rule in active:
        if not rule.get("id"):
            continue
        if rule["kind"] == "block_mention" and any(reason in [*names, *reasons] for reason in (
            f"禁止用户名：@{normalize(rule['value']).lstrip('@')}",
            f"禁止引流账号：@{normalize(rule['value']).lstrip('@')}")):
            hit_ids.append(rule["id"])
        if rule["kind"] == "block_domain" and any(
            reason.startswith("禁止域名：") and domain_matches(reason.split("：", 1)[1], rule["value"])
            for reason in reasons):
            hit_ids.append(rule["id"])
    return {"reasons": list(dict.fromkeys(reasons)), "name_reasons": list(dict.fromkeys(names)),
            "mute_reasons": list(dict.fromkeys(mute_reasons)), "ban_reasons": ban_reasons,
            "rule_ids": list(dict.fromkeys(hit_ids))}


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
