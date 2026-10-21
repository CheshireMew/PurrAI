"""Recovery of CAPTCHA cleanup intents and bounded moderation state."""

import asyncio
import logging

from purrai.diagnostics import failure
from purrai.models import (
    now,
)
from purrai.state_retention import prune_expired_fingerprints

logger = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, db, telegram, settings, cleanup, run_one):
        self.db, self.tg, self.settings = db, telegram, settings
        self.cleanup, self.run_one = cleanup, run_one

    async def cleanup_loop(self):
        next_reconcile = 0
        while True:
            try:
                if not self.tg.connected or not self.tg.user_id:
                    await asyncio.sleep(0.3)
                    continue
                if now() >= next_reconcile:
                    await self.cleanup.reconcile_verification_cleanup()
                    async with self.db.transaction() as session:
                        await prune_expired_fingerprints(session)
                    next_reconcile = now() + 30
                if not await self.run_one():
                    await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "验证码清理循环异常", terminal=True)
                await asyncio.sleep(2)
