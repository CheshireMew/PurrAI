"""Review imported historical samples without applying member punishments."""

import asyncio

from sqlalchemy import select

from purrai.advertisement_content import include_number_readings
from purrai.advertisement_store import policy, rescan_pending
from purrai.chat_budget import BudgetExceeded
from purrai.chat_model import ChatModelError
from purrai.models import AdvertisementSample, Group, Meta, now
from purrai.shared_policy import effective_rules


class HistoricalSampleReview:
    def __init__(self, db, requests):
        self.db, self.requests = db, requests

    async def run_one(self):
        """Confirm imported evidence in batches; historical review never punishes members."""
        async with self.db.transaction() as session:
            first = await session.scalar(
                select(AdvertisementSample)
                .join(Group)
                .where(
                    AdvertisementSample.review_state == "pending",
                    AdvertisementSample.review_after <= now(),
                    AdvertisementSample.enabled.is_(True),
                    Group.approved.is_(True),
                    Group.enabled.is_(True),
                )
                .order_by(AdvertisementSample.created_at)
                .limit(1)
            )
            if not first:
                return False
            cfg, policy_version = await policy(session, first.group_id)
            if not cfg.enabled:
                first.review_after = now() + 60
                return False
            rows = (
                await session.scalars(
                    select(AdvertisementSample)
                    .where(
                        AdvertisementSample.group_id == first.group_id,
                        AdvertisementSample.review_state == "pending",
                        AdvertisementSample.enabled.is_(True),
                        AdvertisementSample.review_after <= now(),
                    )
                    .order_by(AdvertisementSample.created_at)
                    .limit(cfg.batch_size)
                )
            ).all()
            rows = [
                row
                for row in rows
                if not row.violation_id
                or not await session.get(Meta, "violation-correction:" + row.violation_id)
            ]
            if not rows:
                first.review_state, first.review_error = "cancelled", "原判定已纠正"
                return True
            for row in rows:
                row.review_state, row.review_attempts = "requesting", row.review_attempts + 1
            rules = await effective_rules(session, first.group_id)
        snapshots = {row.id: row.version for row in rows}
        error, quota = "模型未返回该历史样本的完整结论", False
        try:
            messages = [
                include_number_readings({"id": row.id, "name": row.sender_name, "text": row.text})
                for row in rows
            ]
            from purrai.advertisement_store import criteria_version

            criteria = criteria_version(rules)
            decisions = await self.requests.review(
                first.group_id,
                rules,
                messages,
                attempts={row.id: row.review_attempts for row in rows},
                history=True,
            )
            async with self.db.transaction() as session:
                cfg, version = await policy(session, first.group_id)
                for decision in decisions:
                    row = await session.get(AdvertisementSample, decision["id"])
                    if row.version != snapshots[row.id] or row.review_state != "requesting":
                        continue
                    if (
                        not cfg.enabled
                        or version != policy_version
                        or criteria != criteria_version(await effective_rules(session, first.group_id))
                    ):
                        continue
                    corrected = row.violation_id and await session.get(
                        Meta, "violation-correction:" + row.violation_id
                    )
                    row.enabled = decision["advertisement"] and not corrected
                    row.scope, row.reason = decision["scope"], decision["reason"][:500]
                    row.source, row.review_state, row.review_error = "model", "done", ""
                    row.version, row.updated_at = row.version + 1, now()
                    if row.enabled and row.scope == "text":
                        await rescan_pending(session, row.group_id)
        except asyncio.CancelledError:
            error = "历史样本复核中断"
            raise
        except BudgetExceeded as exc:
            error, quota = str(exc), True
        except Exception as exc:
            error = (
                str(exc) if isinstance(exc, ChatModelError) else "历史样本复核失败，可在样本编辑中重新确认"
            )
        finally:
            async with self.db.transaction() as session:
                for identifier in snapshots:
                    row = await session.get(AdvertisementSample, identifier)
                    if row.review_state != "requesting" or row.version != snapshots[identifier]:
                        continue
                    row.review_state = "pending" if quota or row.review_attempts < 5 else "failed"
                    row.review_error = error
                    row.review_after = now() + (3600 if quota else min(60, 2**row.review_attempts))
                    if quota:
                        row.review_attempts -= 1
        return True
