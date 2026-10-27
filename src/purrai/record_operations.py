"""Record-level operation choices shared by administration read models."""

from purrai.verification_state import ACTIVE

PERMANENT_BANS = ("contact_ban", "emoji_status_ban", "advertisement_ban")


def retryable_verification(state, failure_action):
    return state == "failed" and failure_action == "permanent_mute"


def verification_operations(record):
    if record["state"] in ACTIVE:
        return ["release", "reissue", "ban"]
    if retryable_verification(record["state"], record["failure_action"]):
        return ["release", "reissue"]
    if record["state"] == "failed" and record["failure_action"] in ("decline", "legacy_ban", "permanent_ban"):
        return ["unban"]
    return []


def permanently_banned(actions):
    return any(action["kind"] in PERMANENT_BANS for action in actions)


def violation_operations(record, permanent_ban):
    if record["sender_chat"]:
        return []
    result = ["revoke_warning"] if record["warning_active"] else []
    if permanent_ban:
        return result + ["unban"]
    if record["profile_violation"] or record["rule_mute"] or record["warning_active"]:
        result.append("unmute")
    if record["warning_active"]:
        result.append("unban")
    return result
