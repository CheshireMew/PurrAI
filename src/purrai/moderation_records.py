"""Association shared by the web and Telegram violation views."""

from sqlalchemy import or_, select

from purrai.models import Action, Meta, RuleHit, Violation, now, uid
from purrai.reason_policy import classifies, details, unique_reasons
from purrai.shared_policy import effective_rules


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
    reasons = unique_reasons(reasons)
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
            reason_details={str(reason): details(reason) for reason in reasons if details(reason)},
            warning_active=False,
            **flags,
        )
        session.add(row)
        await session.flush()
        return row, True
    if await session.get(Meta, f"violation-correction:{row.id}"):
        return row, False
    rules = await effective_rules(session, group_id)
    changed = any(value and not getattr(row, key) for key, value in flags.items())
    for category in ("contact", "ban"):
        changed = changed or (any(classifies(reason, category, rules) for reason in reasons)
            and not any(classifies(reason, category, rules, row.reason_details) for reason in row.reasons))
    row.reason_details = {**(row.reason_details or {}),
                          **{str(reason): details(reason) for reason in reasons if details(reason)}}
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


async def record_rule_hits(session, group_id, message_id, identifiers):
    from sqlalchemy.dialects.sqlite import insert

    if identifiers:
        await session.execute(insert(RuleHit).values([
            {"group_id": group_id, "message_id": message_id, "rule_id": identifier, "created_at": now()}
            for identifier in set(identifiers)
        ]).on_conflict_do_nothing())
