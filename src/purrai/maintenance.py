"""Recovery of CAPTCHA cleanup intents and bounded moderation state."""

import asyncio
import logging

from purrai.diagnostics import failure
from purrai.models import (
    now,
)
from purrai.state_retention import prune_expired_fingerprints, prune_temporary_state
from purrai.notice_cleanup import NoticeCleanup

logger = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, db, telegram, settings, cleanup, run_one):
        self.db, self.tg, self.settings = db, telegram, settings
        self.cleanup, self.run_one = cleanup, run_one
        self.notices = NoticeCleanup(db)

    async def _database_work(self, work):
        async with self.db.transaction() as session:
            return await work(session)

    async def _periodic(self, deadlines, name, work, batch=None):
        if now() < deadlines.get(name, 0):
            return
        try:
            count = await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure(logger, self.settings, exc, f"{name}异常", terminal=True)
            deadlines[name] = now() + 2
        else:
            # A full batch means more eligible work may remain. Share the
            # next loop with other jobs instead of sleeping a full interval.
            deadlines[name] = now() if batch and count is not None and count >= batch else now() + 30

    async def cleanup_loop(self):
        from purrai.points_rewards import refund_expired
        from purrai.lottery import cycle as lottery_cycle
        from purrai.model_diagnostics import ModelDiagnostics
        from purrai.operational_retention import prune_operational_state

        deadlines = {}
        model_diagnostics = ModelDiagnostics(self.settings)
        while True:
            try:
                await self._periodic(deadlines, "积分红包退款", lambda: self._database_work(refund_expired), 100)
                await self._periodic(deadlines, "抽奖维护", lambda: self._database_work(lottery_cycle), 100)
                await self._periodic(deadlines, "指纹清理", lambda: self._database_work(prune_expired_fingerprints))
                await self._periodic(deadlines, "临时状态清理", lambda: self._database_work(prune_temporary_state), 250)
                await self._periodic(deadlines, "审核运行副本清理", lambda: self._database_work(
                    lambda session: prune_operational_state(session, self.settings)), 250)
                await self._periodic(deadlines, "模型诊断清理", lambda: asyncio.to_thread(model_diagnostics.prune))
                if not self.tg.connected or not self.tg.user_id:
                    await asyncio.sleep(0.3)
                    continue
                await self._periodic(deadlines, "验证码清理恢复", self.cleanup.reconcile_verification_cleanup, 100)
                await self._periodic(deadlines, "通知清理恢复", self.notices.reconcile, 100)
                if not await self.run_one():
                    await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "清理任务执行异常", terminal=True)
                await asyncio.sleep(2)
