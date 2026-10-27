"""Bounded, per-group command evidence and remembered model advertising verdicts."""

import hashlib
import json
import re

from purrai.chat_input import input_reasons
from purrai.models import Meta, now
from purrai.rules import normalize

WINDOW = 7 * 86400
MAX_COMMANDS = 200
MAX_EVENTS = 100
MIN_USERS = 3
POINTS = re.compile(r"(?:发)?积分红包\s*\d+(?:\.\d+)?(?:\s+\d+)?")
OTHER_BOT = re.compile(r"@([a-z0-9_]+bot)\s+(.+)", re.I)
BOT_COMMAND = re.compile(r"/?([a-z][a-z0-9_]*)(?:\s+[a-z0-9_.:-]+)*", re.I)
FUNCTION = re.compile(
    r"(?:查询|查看|领取|兑换|刷新|我的|每日|今日|个人|群)?"
    r"(?:积分|余额|金币|贡献|等级|经验|账户|背包|红包|抽奖|转盘|任务|奖励|签到|运势)"
    r"(?:查询|排行|排名|明细|记录|中心|列表|商店|统计|转盘)?"
)


def canonical(text):
    return "".join(normalize(text).split())


def utility_command(text, bot_username=""):
    value = normalize(text)
    if POINTS.fullmatch(value) or POINTS.fullmatch(canonical(value)) or canonical(value) == "积分排行":
        return True
    match = OTHER_BOT.fullmatch(value)
    if not match or match[1].casefold() == bot_username.casefold().lstrip("@"):
        return False
    command = BOT_COMMAND.fullmatch(match[2])
    return bool(POINTS.fullmatch(match[2]) or candidate(match[2])
                or command and (match[2].startswith("/") or "_" in command[1]
                                or command[1] in {"start", "help", "balance", "checkin"}))


def candidate(text):
    value = canonical(text)
    if len(value) > 80:
        return ""
    value = value.rstrip("0123456789.")
    # Count whole functional phrases, never individual words inside conversation.
    if len(value) > 20 or not FUNCTION.fullmatch(value):
        return ""
    return value


def ad_key(text):
    return hashlib.sha256(canonical(text).encode()).hexdigest()


def known_ad(data, text):
    phrase = candidate(text)
    return ad_key(text) in data["ads"] or bool(phrase and ad_key(phrase) in data["ads"])


async def load(session, group_id):
    row = await session.get(Meta, f"chat_input:{group_id}")
    data = json.loads(row.value) if row else {"commands": {}, "ads": {}}
    cutoff = now() - WINDOW
    data["ads"] = {key: at for key, at in data.get("ads", {}).items() if at >= cutoff}
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


def learned(data, cfg):
    exceptions = {canonical(text) for text in cfg.chat_command_exceptions}
    return [phrase for phrase, events in data["commands"].items()
            if cfg.chat_command_learning and phrase not in exceptions
            and len(events) >= cfg.chat_command_threshold
            and len({event[1] for event in events}) >= MIN_USERS
            and ad_key(phrase) not in data["ads"]]


async def observe(session, group_id, message, cfg, *, eligible, edited=False, advertising=False):
    row, data = await load(session, group_id)
    phrase = candidate(message.get("text") or message.get("caption") or "")
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
          and now() - WINDOW <= message.get("date", now()) <= now() + 60):
        data["commands"].setdefault(phrase, []).append([message_id, message["from"]["id"], message.get("date", now())])
    if row or data["commands"] or data["ads"]:
        save(session, row, group_id, data)
    return data


async def remember_ad(session, group_id, message_id, text):
    row, data = await load(session, group_id)
    data["ads"][ad_key(text)] = now()
    phrase = candidate(text)
    if phrase:
        data["ads"][ad_key(phrase)] = now()
        data["commands"].pop(phrase, None)
    for key in list(data["commands"]):
        data["commands"][key] = [event for event in data["commands"][key] if event[0] != message_id]
        if not data["commands"][key]:
            del data["commands"][key]
    save(session, row, group_id, data)


def status(data, cfg, rules=()):
    active = set(learned(data, cfg))
    return {"enabled": cfg.chat_command_learning, "threshold": cfg.chat_command_threshold,
            "minimum_users": MIN_USERS, "window_days": WINDOW // 86400,
            "items": [{"text": phrase, "count": len(events), "users": len({event[1] for event in events}),
                       "ignored": phrase in active}
                      for phrase, events in sorted(data["commands"].items(), key=lambda item: (-len(item[1]), item[0]))
                      if not input_reasons(phrase, list(rules), cfg.model_dump())]}
