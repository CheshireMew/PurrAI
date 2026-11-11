"""Supervise long-lived workers, retain diagnostics and shut them down together."""

import asyncio
import logging

from purrai.diagnostics import failure
from purrai.models import now

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    def __init__(self, settings):
        self.settings = settings
        self.tasks, self.states = [], {}
        self.stopping = False

    def start(self, workers):
        self.stopping = False
        self.tasks = [asyncio.create_task(self.supervise(name, run), name=name) for name, run in workers]
        for task in self.tasks:
            task.add_done_callback(self.worker_finished)

    async def supervise(self, name, run):
        status = self.states.setdefault(name, {"name": name, "state": "running", "error": "", "restarts": 0})
        while not self.stopping:
            status["state"] = "running"
            try:
                await run()
                if self.stopping:
                    return
                raise RuntimeError("常驻任务意外结束")
            except asyncio.CancelledError:
                status["state"] = "stopped"
                raise
            except Exception as exc:
                status.update(state="retrying", restarts=status["restarts"] + 1,
                              error=failure(logger, self.settings, exc, "后台任务恢复重试", f"worker={name}", terminal=True))
                status["retry_at"] = now() + min(60, 2 ** min(status["restarts"], 6))
                await asyncio.sleep(max(0, status["retry_at"] - now()))

    def ready(self):
        return bool(self.tasks) and all(not task.done() for task in self.tasks) and all(
            status["state"] == "running" for status in self.states.values())

    def status(self, last_error=""):
        errors = [row["error"] for row in self.states.values() if row.get("error")]
        return {"workers": list(self.states.values()), "runtime_ready": self.ready(),
                "last_error": "；".join([*errors, *([last_error] if last_error else [])])}

    def worker_finished(self, task):
        if task.cancelled() or self.stopping:
            return
        error = task.exception() or RuntimeError("常驻任务意外结束")
        self.states[task.get_name()] = {"name": task.get_name(), "state": "failed", "restarts": 0,
            "error": failure(logger, self.settings, error, "后台任务退出", f"worker={task.get_name()}", terminal=True)}

    async def stop(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
