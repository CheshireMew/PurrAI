"""Bounded, per-group command evidence and remembered model advertising verdicts."""

import hashlib
import json
import re

from purrai.chat_input import input_reasons
from purrai.models import Group, Meta, now
from purrai.schemas import GroupSettings
from purrai.rules import normalize
from purrai.message_identity import profile_name
from purrai.recognition_policy import matches
from purrai.command_text import normalized_command
from purrai.shared_policy import effective_rules
from purrai.policy_work import policy_work

WINDOW = GroupSettings().chat_evidence_hours * 3600  # Initial-policy compatibility export.
MAX_COMMANDS = 200
MAX_EVENTS = 100
OTHER_BOT = re.compile(r"@([a-z0-9_]+bot)\s+(.+)", re.I)
BOT_COMMAND = re.compile(r"/?([a-z][a-z0-9_]*)(?:\s+[a-z0-9_.:-]+)*", re.I)


def canonical(text):
    return "".join(normalized_command(normalize(text)).split())


def utility_command(text, bot_username="", *, rules=None):
    value = normalized_command(normalize(text))
    if matches(rules, "utility_patterns", value) or matches(rules, "utility_patterns", canonical(value)):
        return True
    match = OTHER_BOT.fullmatch(value)
    if not match or match[1].casefold() == bot_username.casefold().lstrip("@"):
        return False
    command = BOT_COMMAND.fullmatch(match[2])
    return bool(matches(rules, "utility_patterns", match[2]) or candidate(match[2], rules)
                or command and matches(rules, "other_bot_patterns", command[1]))


def candidate(text, rules=None):
    value = canonical(text)
    if len(value) > 80:
        return ""
    value = value.rstrip("0123456789.")
    # Count whole functional phrases, never individual words inside conversation.
    if len(value) > 20 or not matches(rules, "candidate_patterns", value):
        return ""
    return value


def ad_key(text):
    # Historical advertising evidence keeps its original fingerprint. Script
    # equivalence is for command recognition, not a rewrite of saved ad keys.
    return hashlib.sha256("".join(normalize(text).split()).encode()).hexdigest()


def known_ad(data, text, sender_id=None, sender_name="", *, rules=None):
    phrase = candidate(text, rules)
    keys = [ad_key(text)] + ([ad_key(phrase)] if phrase else [])
    scoped = [f"member:{sender_id}:{ad_key(sender_name)}:{key}" for key in keys] if sender_id else []
    return any(key in data["ads"] for key in keys + scoped)


async def load(session, group_id):
    group = await session.get(Group, group_id)
    cfg = GroupSettings.model_validate(group.settings if group else {})
    row = await session.get(Meta, f"chat_input:{group_id}")
    data = json.loads(row.value) if row else {"commands": {}, "ads": {}}
    cutoff = now() - cfg.chat_evidence_hours * 3600
    advertiser_cutoff = now() - max(cfg.chat_evidence_hours * 3600, cfg.chat_advertiser_cooldown_seconds)
    data["ads"] = {key: at for key, at in data.get("ads", {}).items() if at >= cutoff}
    data["advertisers"] = {key: value for key, value in data.get("advertisers", {}).items()
                           if (value.get("at", 0) if isinstance(value, dict) else value) >= advertiser_cutoff}
    commands = {}
    for phrase, events in data.get("commands", {}).items():
        events = [event for event in events if event[2] >= cutoff]
        if events:
            commands[phrase] = events[-MAX_EVENTS:]
    data["commands"] = commands
    return row, data


def save(session, row, group_id, data):
    data["commands"] = {phrase: events[-MAX_EVENTS:] for phrase, events in data["commands"].items() if events}
    data["commands"] = dict(sorted(data["commands"].items(), key=lambda item: item[1][-1][2])[-MAX_COMMANDS:])
    data["ads"] = dict(sorted(data["ads"].items(), key=lambda item: item[1])[-MAX_COMMANDS:])
    value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if row and row.value != value:
        row.value = value
    elif row is None:
        session.add(Meta(key=f"chat_input:{group_id}", value=value))
    return data


def learned(data, cfg, rules=None):
    exceptions = {canonical(text) for text in cfg.chat_command_exceptions}
    return [phrase for phrase, events in data["commands"].items()
            if cfg.chat_command_learning and phrase not in exceptions and candidate(phrase, rules)
            and len(events) >= cfg.chat_command_threshold
            and len({event[1] for event in events}) >= cfg.chat_command_min_users
            and ad_key(phrase) not in data["ads"]]


async def observe(session, group_id, message, cfg, *, eligible, edited=False, advertising=False, rules=None):
    if rules is None:
        rules = await effective_rules(session, group_id)
    row, data = await load(session, group_id)
    eligible = eligible and not (cfg.chat_advertiser_cooldown and advertiser_blocked(data, message.get("from", {}).get("id"), cfg))
    text = message.get("text") or message.get("caption") or ""
    is_ad, phrase = await policy_work(session, lambda: (
        known_ad(data, text, message.get("from", {}).get("id"), profile_name(message.get("from") or {}), rules=rules),
        candidate(text, rules)))
    eligible = eligible and not is_ad
    if advertising and phrase:
        data["commands"].pop(phrase, None)
    message_id = message["message_id"]
    previous = next((event for events in data["commands"].values()
                     for event in events if event[0] == message_id), None)
    if edited or not eligible or previous:
        # Edits remove old evidence but never become a new occurrence.
        if edited or not eligible:
            for key in list(data["commands"]):
                data["commands"][key] = [event for event in data["commands"][key] if event[0] != message_id]
                if not data["commands"][key]:
                    del data["commands"][key]
    elif (cfg.chat_command_learning and phrase and eligible and ad_key(phrase) not in data["ads"]
          and now() - cfg.chat_evidence_hours * 3600 <= message.get("date", now()) <= now() + 60):
        data["commands"].setdefault(phrase, []).append([message_id, message["from"]["id"], message.get("date", now())])
    if row or data["commands"] or data["ads"]:
        save(session, row, group_id, data)
    return data


def advertiser_blocked(data, sender_id, cfg=None):
    cfg = cfg or GroupSettings()
    value = data.get("advertisers", {}).get(str(sender_id), {})
    if not cfg.chat_advertiser_cooldown_seconds:
        return False
    # Old receipts already carry an expiry. The migration converts them to facts.
    until = value.get("at", 0) + cfg.chat_advertiser_cooldown_seconds if isinstance(value, dict) else value
    return until > now()


async def remember_advertiser(session, group_id, sender_id):
    if type(sender_id) is not int or sender_id <= 0:
        return
    row, data = await load(session, group_id)
    data["advertisers"][str(sender_id)] = {"at": now()}
    data["advertisers"] = dict(sorted(data["advertisers"].items(),
        key=lambda item: item[1].get("at", 0) if isinstance(item[1], dict) else item[1], reverse=True)[:MAX_COMMANDS])
    save(session, row, group_id, data)


async def remember_ad(session, group_id, message_id, text, sender_id=None, *, sender_name="", cooldown=False):
    phrase = await policy_work(session, candidate, text, await effective_rules(session, group_id))
    row, data = await load(session, group_id)
    prefix = f"member:{sender_id}:{ad_key(sender_name)}:" if sender_id else ""
    data["ads"][prefix + ad_key(text)] = now()
    if phrase:
        data["ads"][prefix + ad_key(phrase)] = now()
        if not sender_id:
            data["commands"].pop(phrase, None)
    for key in list(data["commands"]):
        data["commands"][key] = [event for event in data["commands"][key] if event[0] != message_id]
        if not data["commands"][key]:
            del data["commands"][key]
    save(session, row, group_id, data)
    if cooldown:
        await remember_advertiser(session, group_id, sender_id)


def status(data, cfg, rules=()):
    active = set(learned(data, cfg, rules))
    return {"enabled": cfg.chat_command_learning, "threshold": cfg.chat_command_threshold,
            "minimum_users": cfg.chat_command_min_users, "window_days": cfg.chat_evidence_hours / 24,
            "items": [{"text": phrase, "count": len(events), "users": len({event[1] for event in events}),
                       "ignored": phrase in active}
                      for phrase, events in sorted(data["commands"].items(), key=lambda item: (-len(item[1]), item[0]))
                      if not input_reasons(phrase, rules, cfg.model_dump())]}
