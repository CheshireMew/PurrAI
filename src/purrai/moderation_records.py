"""Association shared by the web and Telegram violation views."""

from sqlalchemy import or_, select

from purrai.models import Action


async def violation_actions(session, group_id, violation_id):
    return (await session.scalars(select(Action).where(
        Action.group_id == group_id,
        or_(Action.payload["violation_id"].as_string() == violation_id,
            Action.dedupe_key == f"warning:{violation_id}"),
    ).order_by(Action.created_at, Action.id))).all()
