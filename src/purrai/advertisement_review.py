"""Compose advertising review services and coordinate durable real-time work."""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from purrai.advertisement_context import prepare_review
from purrai.advertisement_content import prompt as prompt
from purrai.advertisement_decisions import AdvertisementDecisions
from purrai.advertisement_history import HistoricalSampleReview
from purrai.advertisement_intake import AdvertisementIntake
from purrai.advertisement_model import AdvertisingModelReview
from purrai.advertisement_store import policy, wake_chat_reward
from purrai.chat_budget import BudgetExceeded
from purrai.chat_model import ChatModelError
from purrai.message_parts import message_text
from purrai.models import AdvertisementReview, AdvertisementSample, now
from purrai.points_trust import exempt_review
from purrai.policy_work import PolicyExecution

logger = logging.getLogger(__name__)


class AdvertisementReviewer:
    def __init__(self, db, telegram, settings, model=None):
        self.db, self.tg, self.settings = db, telegram, settings
        self.requests = AdvertisingModelReview(db, settings, model)
        self.decisions = AdvertisementDecisions(db, telegram, settings)
        self.history = HistoricalSampleReview(db, self.requests)
        self.wakeup = asyncio.Event()
        self.intake = AdvertisementIntake(db, telegram, settings, self.decisions, self.wakeup)
        self.local_checks = self.intake.local_checks
        self.busy = set()
        self.claim_lock = asyncio.Lock()
        self.last_group = None
        self.started_at = now()

    @property
    def model(self):
        return self.requests.model

    @model.setter
    def model(self, model):
        self.requests.model = model

    @property
    def budget(self):
        return self.requests.budget

    async def observe(self, message, edited=False, update_id=0, *, chat_routing=False):
        return await self.intake.observe(message, edited, update_id, chat_routing=chat_routing)

    async def local(self, identifier):
        return await self.intake.local(identifier)

    async def check_samples(self, identifier):
        return await self.intake.check_samples(identifier)

    async def valid(self, session, row, *, allow_expired=False):
        return await self.decisions.valid(session, row, allow_expired=allow_expired)

    async def apply(self, identifier, decision, policy_version, *, match=None, hints=None, criteria=None):
        return await self.decisions.apply(
            identifier, decision, policy_version, match=match, hints=hints, criteria=criteria
        )

    async def review_history(self):
        return await self.history.run_one()

    async def claim(self):
        async with self.claim_lock:
            async with self.db.transaction() as session:
                # A commit can finish before its caller receives the handoff.
                # Reclaim only independent reviews without an active owner;
                # shared chat reviews belong to their ChatTurn instead.
                await session.execute(
                    update(AdvertisementReview)
                    .where(
                        AdvertisementReview.state == "requesting",
                        AdvertisementReview.group_id.not_in(self.busy),
                        AdvertisementReview.message["_chat_turn_id"].as_integer().is_(None),
                    )
                    .values(state="pending", available_at=now(), error="恢复未完成的审核领取")
                )
                claimed = await self.select_batch(session)
            if claimed:
                gid = claimed[0][0].group_id
                self.busy.add(gid)
                self.last_group = gid
            return claimed

    async def select_batch(self, session):
        ready = (
            AdvertisementReview.state == "pending",
            AdvertisementReview.available_at <= now(),
            AdvertisementReview.local_after > now(),
            AdvertisementReview.id.not_in(self.local_checks),
            AdvertisementReview.message["_chat_routing"].as_boolean().is_not(True),
            AdvertisementReview.message["_chat_turn_id"].as_integer().is_(None),
        )
        order = [func.min(AdvertisementReview.created_at), AdvertisementReview.group_id]
        if self.last_group is not None:
            order = [AdvertisementReview.group_id <= self.last_group, AdvertisementReview.group_id]
        scopes = (
            await session.scalars(
                select(AdvertisementReview.group_id)
                .where(*ready)
                .group_by(AdvertisementReview.group_id)
                .order_by(*order)
            )
        ).all()
        for gid in scopes:
            if gid in self.busy:
                continue
            cfg, version = await policy(session, gid)
            rows = (
                await session.scalars(
                    select(AdvertisementReview)
                    .where(AdvertisementReview.group_id == gid, *ready)
                    .order_by(AdvertisementReview.created_at, AdvertisementReview.id)
                    .limit(cfg.batch_size)
                )
            ).all()
            batch, size = [], 0
            for row in rows:
                if await exempt_review(session, row):
                    continue
                valid, _, _, _ = await self.valid(session, row)
                if not valid:
                    row.state, row.error = "cancelled", "消息、权限或审核设置已失效"
                    await wake_chat_reward(session, row)
                    continue
                length = len(message_text(row.message))
                if batch and size + length > 12000:
                    break
                size += length
                row.state, row.attempts = "requesting", row.attempts + 1
                batch.append(row)
            if batch:
                return batch, version
        return None

    async def defer(self, identifiers, reason, *, quota=False, terminal=False):
        async with self.db.transaction() as session:
            for identifier in identifiers:
                row = await session.get(AdvertisementReview, identifier)
                if row.state != "requesting":
                    continue
                row.error, row.updated_at = reason, now()
                row.state = "failed" if terminal or row.attempts >= 5 and not quota else "pending"
                if row.state == "failed":
                    await wake_chat_reward(session, row)
                if quota:
                    tomorrow = datetime.fromtimestamp(now(), ZoneInfo("Asia/Shanghai")).date() + timedelta(
                        days=1
                    )
                    row.available_at = datetime.combine(
                        tomorrow, datetime.min.time(), ZoneInfo("Asia/Shanghai")
                    ).timestamp()
                    row.attempts -= 1
                else:
                    row.available_at = now() + min(60, 2**row.attempts)

    async def run_one(self):
        from purrai.advertisement_routing import recover_routing

        async with self.db.transaction() as session:
            await recover_routing(session)
        # Recheck pending rows locally: a prior request may just have learned their template.
        async with self.db.sessions() as session:
            ids = (
                await session.scalars(
                    select(AdvertisementReview.id)
                    .where(
                        AdvertisementReview.state == "pending",
                        AdvertisementReview.local_after <= now(),
                        AdvertisementReview.id.not_in(self.local_checks),
                    )
                    .order_by(
                        AdvertisementReview.local_after,
                        AdvertisementReview.created_at,
                        AdvertisementReview.id,
                    )
                    .limit(24)
                )
            ).all()
        for identifier in ids:
            await self.local(identifier)
        if not self.settings.chat_ready:
            return bool(ids)
        claimed = await self.claim()
        if not claimed:
            return await self.review_history() or bool(ids)
        rows, version = claimed
        gid, identifiers = rows[0].group_id, [row.id for row in rows]
        try:
            async with PolicyExecution(self.db) as session:
                request, hints, rules = await prepare_review(session, rows)
            from purrai.advertisement_store import criteria_version

            criteria = criteria_version(rules)
            decisions = await self.requests.review(
                gid, rules, request, attempts={row.id: row.attempts for row in rows}
            )
            seen = {decision["id"] for decision in decisions}
            messages = {item["id"]: item for item in request}
            for decision in decisions:
                decision = {**decision, "reason": decision["reason"][:500]}
                if decision["scope"] == "bio":
                    decision["scope"] = "context"
                if decision["advertisement"] and messages[decision["id"]]["bio"]:
                    decision["scope"] = "context"
                await self.apply(
                    decision["id"], decision, version, hints=hints[decision["id"]], criteria=criteria
                )
            await self.defer(set(identifiers) - seen, "模型漏答此消息，等待重试")
        except asyncio.CancelledError:
            await self.defer(identifiers, "审核中断，等待恢复")
            raise
        except BudgetExceeded as exc:
            await self.defer(identifiers, str(exc), quota=True)
        except Exception as exc:
            logger.warning("广告审核暂未完成 group=%s reason=%s", gid, type(exc).__name__)
            await self.defer(
                identifiers,
                str(exc) if isinstance(exc, ChatModelError) else "审核连接失败，等待重试",
                terminal=isinstance(exc, ChatModelError) and not exc.retryable,
            )
        finally:
            self.busy.discard(gid)
        return True

    async def run(self):
        async with self.db.transaction() as session:
            from purrai.advertisement_routing import recover_routing

            await recover_routing(session, startup_before=self.started_at)
            await session.execute(
                update(AdvertisementReview)
                .where(AdvertisementReview.state == "requesting")
                .values(state="pending", available_at=now(), error="恢复未完成审核")
            )
            await session.execute(
                update(AdvertisementSample)
                .where(AdvertisementSample.review_state == "requesting")
                .values(review_state="pending", review_after=now(), review_error="恢复历史样本复核")
            )
        while True:
            try:
                self.wakeup.clear()
                if not await self.run_one():
                    try:
                        await asyncio.wait_for(self.wakeup.wait(), 0.5)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("广告审核循环异常")
                await asyncio.sleep(2)

    async def close(self):
        await self.requests.close()
