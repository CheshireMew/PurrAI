"""Per-group privileges retained after the member's recorded balance reaches a threshold."""
from dataclasses import dataclass

from sqlalchemy import func, select

from purrai.models import PointsAccount, PointsDaily, PointsEntry
from purrai.points_policy import get_policy


@dataclass(frozen=True)
class PointsTrust:
    earned_points: int = 0
    active_days: int = 0
    level: int = 0

    @property
    def newcomer(self):
        return self.level >= 1

    @property
    def advertising(self):
        return self.level >= 2


async def members_trust(session, group_id, user_ids, policy=None):
    policy = policy or await get_policy(session, group_id)
    identifiers = {value for value in user_ids if value and value > 0}
    if not identifiers:
        return {}
    days = select(func.count()).select_from(PointsDaily).where(
        PointsDaily.group_id == group_id, PointsDaily.user_id == PointsAccount.user_id,
        PointsDaily.chat_points > 0,
    ).correlate(PointsAccount).scalar_subquery()
    rows = await session.execute(select(PointsAccount.user_id, func.max(PointsEntry.balance_after), days).join(
        PointsEntry, PointsEntry.account_id == PointsAccount.id,
    ).where(PointsAccount.group_id == group_id, PointsEntry.group_id == group_id,
        PointsAccount.user_id.in_(identifiers), PointsEntry.kind != "identity",
        PointsEntry.result["user_id"].as_integer() == PointsAccount.user_id,
    ).group_by(PointsAccount.user_id))
    result = {identifier: PointsTrust() for identifier in identifiers}
    for identifier, earned, active_days in rows:
        level = 0
        if policy.enabled and policy.trust_enabled and identifier not in policy.trust_excluded_user_ids:
            level = int(earned >= policy.newcomer_exemption_points) + int(earned >= policy.advertising_exemption_points)
        result[identifier] = PointsTrust(earned, active_days, level)
    return result


async def member_trust(session, group_id, user_id):
    policy = await get_policy(session, group_id)
    if not policy.enabled or not policy.trust_enabled or user_id in policy.trust_excluded_user_ids:
        return PointsTrust()
    return (await members_trust(session, group_id, [user_id], policy)).get(user_id, PointsTrust())


async def exempt_review(session, review):
    """Close a review before sample matching or model dispatch when it is exempt."""
    if not (await member_trust(session, review.group_id, review.user_id)).advertising:
        return False
    from purrai.advertisement_store import wake_chat_reward
    from purrai.models import now
    review.state, review.error, review.updated_at = "exempt", "", now()
    review.result = dict(source="points", reason="本群历史积分已达到广告审核豁免门槛")
    await wake_chat_reward(session, review)
    return True
