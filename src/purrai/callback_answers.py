"""Bounded, short-lived Telegram hints; business results are already committed."""

import asyncio
import logging

from purrai.diagnostics import failure

logger = logging.getLogger(__name__)


class CallbackAnswers:
    def __init__(self, telegram, settings):
        self.tg, self.settings = telegram, settings
        self.tasks, self.slots = set(), asyncio.Semaphore(8)
        self.closed = False

    async def send(self, identifier, text="", *, show_alert=False):
        if self.closed:
            return
        if len(self.tasks) >= 64:
            logger.warning("按钮提示回执队列已满 callback=%s", identifier)
            return
        task = asyncio.create_task(self.answer(identifier, text, show_alert=show_alert), name="callback-answer")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        await asyncio.sleep(0)

    async def answer(self, identifier, text="", *, show_alert=False):
        try:
            async with asyncio.timeout(4):
                async with self.slots:
                    await self.tg.call("answer_callback_query", callback_query_id=identifier,
                        text=text[:200], show_alert=show_alert, request_timeout=3)
        except Exception as exc:
            failure(logger, self.settings, exc, "按钮提示回执失败（操作结果已保存）", f"callback={identifier}")

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
