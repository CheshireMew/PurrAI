"""Association shared by the web and Telegram violation views."""

from sqlalchemy import or_, select

from purrai.models import Action, Violation, uid
from purrai.rules import CONTACT_REASONS
from purrai.advertising_patterns import is_ban_reason


async def record_violation(
    session,
    *,
    group_id,
    user_id,
    message_id,
    reasons,
    excerpt="",
    sender_name="",
    sender_chat=False,
    profile_violation=False,
    rule_mute=False,
    message_violation=False,
):
    """Merge message evidence monotonically and report whether penalties changed."""
    row = await session.scalar(
        select(Violation).where(
            Violation.group_id == group_id,
            Violation.message_id == message_id,
        )
    )
    flags = {
        "profile_violation": profile_violation,
        "rule_mute": rule_mute,
        "message_violation": message_violation,
    }
    if row is None:
        row = Violation(
            id=uid(),
            group_id=group_id,
            user_id=user_id,
            message_id=message_id,
            excerpt=excerpt[:600],
            sender_name=sender_name[:500],
            sender_chat=sender_chat,
            reasons=list(dict.fromkeys(reasons)),
            warning_active=False,
            **flags,
        )
        session.add(row)
        await session.flush()
        return row, True
    changed = any(value and not getattr(row, key) for key, value in flags.items()) or (
        any(reason in reasons for reason in CONTACT_REASONS)
        and not any(reason in row.reasons for reason in CONTACT_REASONS)
    )
    changed = changed or (any(is_ban_reason(reason) for reason in reasons)
                          and not any(is_ban_reason(reason) for reason in row.reasons))
    for key, value in flags.items():
        setattr(row, key, getattr(row, key) or value)
    row.reasons = list(dict.fromkeys(row.reasons + reasons))
    return row, changed


async def violation_actions(session, group_id, violation_id):
    return (
        await session.scalars(
            select(Action)
            .where(
                Action.group_id == group_id,
                or_(
                    Action.payload["violation_id"].as_string() == violation_id,
                    Action.dedupe_key == f"warning:{violation_id}",
                ),
            )
            .order_by(Action.created_at, Action.id)
        )
    ).all()
