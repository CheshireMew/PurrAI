"""Per-method Telegram health and credential-free request diagnostics."""

import asyncio
from contextvars import ContextVar
import logging
import time

from aiohttp import TraceConfig
from aiogram.client.session.aiohttp import AiohttpSession

logger = logging.getLogger(__name__)
request_trace = ContextVar("telegram_request_trace", default=None)
request_identity = ContextVar("telegram_request_identity", default="")

METHOD_LABELS = {
    "get_updates": "接收新消息", "get_me": "连接机器人账号", "get_chat": "查询群或用户资料",
    "get_chat_member": "查询成员身份和权限", "send_message": "发送文字消息", "send_photo": "发送图片",
    "delete_message": "删除消息", "restrict_chat_member": "修改成员发言权限",
    "ban_chat_member": "封禁成员", "unban_chat_member": "解除成员封禁",
    "edit_message_reply_markup": "更新消息按钮或核对原消息", "edit_message_text": "更新消息文字",
}


class TracedSession(AiohttpSession):
    """Attach tracing before first I/O, retaining the SDK's proxy/TLS settings."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.trace_lock = asyncio.Lock()
        self.trace = TraceConfig()
        phases = {
            "on_connection_queued_start": "connection_pool", "on_connection_create_start": "connect",
            "on_dns_resolvehost_start": "dns", "on_dns_resolvehost_end": "connect",
            "on_connection_create_end": "upload", "on_connection_reuseconn": "upload",
            "on_request_headers_sent": "response_headers", "on_request_chunk_sent": "response_headers",
            "on_request_end": "response_body", "on_response_chunk_received": "decode",
        }
        for event, phase in phases.items():
            async def mark(session, context, params, value=phase):
                trace = request_trace.get()
                if trace is not None:
                    trace["phase"] = value
            getattr(self.trace, event).append(mark)
        self.trace.freeze()

    async def create_session(self):
        async with self.trace_lock:
            session = await super().create_session()
            if self.trace not in session.trace_configs:
                session.trace_configs.append(self.trace)
            return session


class TelegramHealth:
    def __init__(self):
        self.methods = {}

    def restore(self, status):
        self.methods.update({row["method"]: dict(row) for row in status.get("methods", [])
                             if row.get("consecutive_failures")})

    def record(self, method, outcome, seconds, trace, timeout, error="", retry_after=0):
        at = time.time()
        row = self.methods.setdefault(method, {"method": method, "label": METHOD_LABELS.get(method, method),
            "calls": 0, "failures": 0, "consecutive_failures": 0, "last_success": 0,
            "last_failure": 0, "first_failure": 0, "last_logged": 0})
        row.update(calls=row["calls"] + 1, seconds=round(seconds, 3), phase=trace["phase"],
                   timeout=timeout, outcome=outcome)
        failed = outcome in ("network_error", "unauthorized", "rate_limited", "client_error")
        if failed:
            row["first_failure"] = row["first_failure"] or at
            row.update(failures=row["failures"] + 1, consecutive_failures=row["consecutive_failures"] + 1,
                       last_failure=at, error=error, retry_at=at + retry_after if retry_after else 0)
            if row["consecutive_failures"] == 1 or at - row["last_logged"] >= 60:
                logger.warning("Telegram接口异常 method=%s phase=%s elapsed=%.3fs timeout=%s error=%s consecutive=%s %s",
                    method, trace["phase"], seconds, timeout, error, row["consecutive_failures"], request_identity.get())
                row["last_logged"] = at
        else:
            if row["consecutive_failures"]:
                logger.info("Telegram接口恢复 method=%s elapsed=%.3fs failed_calls=%s %s",
                            method, seconds, row["consecutive_failures"], request_identity.get())
            row.update(last_success=at, first_failure=0, consecutive_failures=0, error="", retry_at=0)
            if outcome == "ok" and method != "get_updates" and seconds >= 5:
                logger.warning("Telegram接口响应较慢 method=%s elapsed=%.3fs timeout=%s %s",
                               method, seconds, timeout, request_identity.get())

    def status(self):
        methods = [dict(row) for row in self.methods.values()]
        problems = [row for row in methods if row["consecutive_failures"]]
        return {"degraded": bool(problems), "problems": problems, "methods": methods}
