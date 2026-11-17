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
request_purpose = ContextVar("telegram_request_purpose", default="operations")

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
        for saved in status.get("methods", []):
            if not saved.get("consecutive_failures"):
                continue
            row = dict(saved)
            # In the preceding release this method with a 3s timeout was used
            # exclusively by source checks. Keep its evidence at the right level.
            purpose = row.setdefault("purpose", "source_check" if row["method"] == "edit_message_reply_markup"
                                     and row.get("timeout") == 3 else "operations")
            row.setdefault("authentication_failed", row.get("outcome") == "unauthorized")
            self.methods[(purpose, row["method"])] = row

    def record(self, method, outcome, seconds, trace, timeout, error="", retry_after=0):
        at = time.time()
        purpose = request_purpose.get()
        row = self.methods.setdefault((purpose, method), {"method": method, "purpose": purpose,
            "label": METHOD_LABELS.get(method, method),
            "calls": 0, "failures": 0, "consecutive_failures": 0, "last_success": 0,
            "last_failure": 0, "first_failure": 0, "last_logged": 0})
        row.update(calls=row["calls"] + 1, seconds=round(seconds, 3), phase=trace["phase"],
                   timeout=timeout, outcome=outcome)
        failed = outcome in ("network_error", "unauthorized", "rate_limited", "client_error")
        row["authentication_failed"] = failed and (outcome == "unauthorized" or row.get("authentication_failed", False))
        auxiliary = purpose in ("source_check", "profile_lookup") and not row["authentication_failed"]
        if failed:
            row["first_failure"] = row["first_failure"] or at
            row.update(failures=row["failures"] + 1, consecutive_failures=row["consecutive_failures"] + 1,
                       last_failure=at, error=error, retry_at=at + retry_after if retry_after else 0)
            if row["consecutive_failures"] == 1 or at - row["last_logged"] >= 60:
                log = logger.debug if auxiliary else logger.warning
                log("Telegram接口异常 method=%s purpose=%s phase=%s elapsed=%.3fs timeout=%s error=%s consecutive=%s %s",
                    method, purpose, trace["phase"], seconds, timeout, error, row["consecutive_failures"], request_identity.get())
                row["last_logged"] = at
        else:
            if row["consecutive_failures"]:
                log = logger.debug if auxiliary else logger.info
                log("Telegram接口恢复 method=%s elapsed=%.3fs failed_calls=%s %s",
                            method, seconds, row["consecutive_failures"], request_identity.get())
            row.update(last_success=at, first_failure=0, consecutive_failures=0, error="", retry_at=0)
            if not auxiliary and outcome == "ok" and method != "get_updates" and seconds >= 5:
                logger.warning("Telegram接口响应较慢 method=%s elapsed=%.3fs timeout=%s %s",
                               method, seconds, timeout, request_identity.get())

    def status(self):
        methods = [dict(row) for row in self.methods.values()]
        failures = [row for row in methods if row["consecutive_failures"]]
        warnings = [row for row in failures if row.get("purpose") in ("source_check", "profile_lookup")
                    and not row.get("authentication_failed") and row["outcome"] != "unauthorized"]
        problems = [row for row in failures if row not in warnings]
        return {"degraded": bool(problems), "problems": problems, "warnings": warnings, "methods": methods}
