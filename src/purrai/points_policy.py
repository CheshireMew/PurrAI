from pathlib import Path

from purrai.models import PointsPolicy
from purrai.points_schemas import PointsSettings


def default_policy():
    return PointsSettings.model_validate_json((Path(__file__).parent / "assets" / "points_policy.v1.json").read_text(encoding="utf-8"))


async def get_policy(session, group_id):
    row = await session.get(PointsPolicy, group_id)
    # Saved empty alias lists and zero rewards must never be reseeded.
    return PointsSettings.model_validate(row.config) if row else default_policy()


async def policy_document(session, group_id):
    row = await session.get(PointsPolicy, group_id)
    return {"config": (await get_policy(session, group_id)).model_dump(), "version": row.version if row else 0}
