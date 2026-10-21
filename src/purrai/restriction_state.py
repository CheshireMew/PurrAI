"""Ownership comparisons shared by all member restriction workflows."""

from purrai.models import Action
from purrai.telegram import permission_snapshot


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
