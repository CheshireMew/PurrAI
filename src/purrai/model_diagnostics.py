"""Opt-in, bounded model exchanges with credential redaction."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

from purrai.diagnostics import safe_text
from purrai.errors import ManagementError

logger = logging.getLogger(__name__)
_context = ContextVar("model_request_context", default=None)
_files_lock = threading.RLock()
_file_name = re.compile(r"[0-9a-f]{32}\.(?:json|tmp)")
_credential_fields = {"apikey", "token", "accesstoken", "refreshtoken", "secret", "clientsecret",
                      "password", "authorization", "cookie", "setcookie"}


@contextmanager
def model_request_context(**values):
    token = _context.set(values)
    try:
        yield
    finally:
        _context.reset(token)


def redact_body(settings, text):
    """Keep exact serialized text unless a credential field needs redaction."""
    changed = False

    def visit(value):
        nonlocal changed
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if re.sub(r"[^a-z0-9]", "", key.lower()) in _credential_fields:
                    result[key], changed = "[redacted]", True
                else:
                    result[key] = visit(item)
            return result
        return [visit(item) for item in value] if isinstance(value, list) else value

    try:
        redacted = visit(json.loads(text))
        if changed:
            text = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        pass
    for name in ("bot_token", "chat_api_key", "oidc_client_secret", "session_secret"):
        secret = getattr(settings, name, "")
        if secret:
            for representation in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
                text = text.replace(representation, "[redacted]")
    return text


class ModelDiagnostics:
    def __init__(self, settings):
        self.settings = settings
        from sqlalchemy.engine import make_url
        self.root = Path(make_url(settings.database_url).database).resolve().parent / "diagnostics" / "model-requests"

    def _files(self):
        if not self.root.is_dir():
            return []
        return [path for path in self.root.iterdir() if _file_name.fullmatch(path.name) and self._owned(path)]

    @staticmethod
    def _owned(path):
        if not path.is_file() or path.is_symlink():
            return False
        prefix = ('{"format":1,"operation":"model_http_exchange","id":"' + path.stem + '"').encode()
        with path.open("rb") as source:
            return source.read(len(prefix)) == prefix

    def _prune(self, incoming=0, current=None):
        cutoff = time.time() - self.settings.model_diagnostics_retention_hours * 3600
        files = []
        for path in self._files():
            if path.stem == current:
                continue
            stat = path.stat()
            if stat.st_mtime <= cutoff:
                path.unlink()
            else:
                files.append((stat.st_mtime, stat.st_size, path))
        budget = self.settings.model_diagnostics_max_mb * 1048576
        total = sum(size for _, size, _ in files) + incoming
        for _, size, path in sorted(files):
            if total <= budget:
                break
            path.unlink()
            total -= size

    def write(self, record):
        with _files_lock:
            value = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(value) > self.settings.model_diagnostics_max_mb * 1048576:
                # Never silently persist a truncated exchange as a full record.
                record = {key: value for key, value in record.items() if key not in ("request", "response")}
                record.update(complete=False, capture_error="完整报文超过诊断容量上限，未保存正文")
                value = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._prune(len(value), record["id"])
            temporary = self.root / (record["id"] + ".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.root / (record["id"] + ".json"))

    async def save(self, record):
        try:
            await asyncio.to_thread(self.write, record)
        except (OSError, ValueError) as exc:
            logger.warning("模型诊断未保存 request=%s · %s", record["id"], safe_text(self.settings, exc))

    async def begin(self, request):
        if not self.settings.model_diagnostics_enabled:
            return None
        record = {"format": 1, "operation": "model_http_exchange", "id": uuid.uuid4().hex,
                  "started_at": time.time(), "state": "requesting",
                  "context": _context.get() or {}, "complete": True,
                  "request": {"method": request.method, "url": safe_text(self.settings, request.url),
                              "body_utf8": redact_body(self.settings, request.content.decode("utf-8"))}}
        await self.save(record)
        return record

    async def finish(self, record, response, error):
        if record is None:
            return
        record.update(finished_at=time.time(), state="cancelled" if isinstance(error, asyncio.CancelledError)
                      else "failed" if error else "succeeded")
        if response is not None:
            record["response"] = {"status_code": response.status_code,
                                  "body_utf8": redact_body(self.settings, response.text)}
        if error:
            record["error"] = {"type": type(error).__name__, "message": safe_text(self.settings, error)}
        await self.save(record)
        logger.info("模型诊断 request=%s state=%s context=%s", record["id"], record["state"], record["context"])

    def page(self, page_number=1):
        with _files_lock:
            self._prune()
            paths = sorted((path for path in self._files() if path.suffix == ".json"),
                           key=lambda path: path.stat().st_mtime, reverse=True)
            items = []
            for path in paths[(page_number - 1) * 100:page_number * 100]:
                value = json.loads(path.read_text(encoding="utf-8"))
                items.append({key: value.get(key) for key in (
                    "id", "started_at", "finished_at", "state", "context", "complete", "capture_error")})
            return {"enabled": self.settings.model_diagnostics_enabled, "items": items, "total": len(paths),
                    "page": page_number, "size": 100,
                    "retention_hours": self.settings.model_diagnostics_retention_hours,
                    "max_mb": self.settings.model_diagnostics_max_mb}

    def read(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ManagementError(404, "诊断记录不存在")
        with _files_lock:
            self._prune()
            path = self.root / (identifier + ".json")
            if not self._owned(path):
                raise ManagementError(404, "诊断记录不存在或已过期")
            return json.loads(path.read_text(encoding="utf-8"))

    def prune(self):
        with _files_lock:
            self._prune()
