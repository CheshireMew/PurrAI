"""Ownership comparisons shared by all member restriction workflows."""

from purrai.models import Action, now
from purrai.telegram import permission_snapshot


def moderation_ownership(member, current, action):
    """A new penalty can add restrictions; a stale retry cannot undo later edits."""
    from purrai.action_execution import RestrictionPreserved
    owned = (member.owned_restriction if member else None) or {}
    consistent = not (member and member.restriction_conflict) and (
        current.get("status") == "restricted" and restriction_matches(current, owned)
    )
    if owned.get("source_action") == action.id:
        before = owned.get("before")
        unsubmitted = bool(before and current.get("status") == owned.get("before_status")
                           and permission_state_matches(current, before["permissions"], before["until_date"]))
        if member.restriction_conflict or not (consistent or unsubmitted or (
            current.get("status") == "member" and 0 < owned.get("until_date", 0) <= now()
        )):
            raise RestrictionPreserved()
        return owned
    return owned if consistent else {}


async def schedule_moderation_expiry(session, group, member):
    from purrai.action_queue import enqueue
    owned = member.owned_restriction
    deadline = owned.get("restore_at") or owned.get("until_date", 0)
    previous_until = owned.get("previous", {}).get("__until_date", 0)
    schedules = []
    if owned.get("restore_at"):
        schedules.append(("moderation-restore", owned["restore_at"], False, "临时禁言到期，恢复禁言前已有的成员限制"))
    if previous_until and (not deadline or previous_until < deadline):
        schedules.append(("prior-restriction-expiry", previous_until, True, "原有限制到期，保留本次禁言"))
    for key, at, prior_expiry, reason in schedules:
        task = await enqueue(session, "unmute", f"{key}:{owned['source_action']}", group.id, member.user_id,
                             {"automatic_restore": True, "expire_previous": prior_expiry,
                              "generation": member.generation, "restriction": restriction_identity(member)},
                             priority=2, reason=reason)
        task.available_at = at


def restriction_identity(member):
    owned = member.owned_restriction if member else None
    return {key: owned.get(key) for key in ("kind", "source_action", "until_date")} if owned else None


def restriction_matches(current, owned, *, tolerance=2):
    return bool(
        owned
        and permission_state_matches(
            current, owned.get("permissions"), owned.get("until_date", 0), tolerance=tolerance
        )
    )


def permission_state_matches(current, permissions, until_date, *, tolerance=2):
    return (
        permission_snapshot(current) == permissions
        and abs(current.get("until_date", 0) - until_date) <= tolerance
    )


def ban_matches(current, until_date, *, tolerance=2):
    return current.get("status") == "kicked" and abs(current.get("until_date", 0) - until_date) <= tolerance


async def owns_verification_ban(session, member, verification):
    if not member or member.generation != verification.generation:
        return False
    owned = member.owned_restriction or {}
    if owned.get("kind") != "ban":
        return False
    if owned.get("verification_id"):
        return owned["verification_id"] == verification.id
    source = await session.get(Action, owned["source_action"]) if owned.get("source_action") else None
    return bool(source and source.payload.get("verification_id") == verification.id)
