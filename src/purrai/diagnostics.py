"""Correlated diagnostics without credentials or SQL parameter dumps."""

import re
import traceback
import logging
from copy import deepcopy
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError


class SafeFormatter(logging.Formatter):
    def __init__(self, settings):
        super().__init__("%(asctime)s %(levelname)s %(name)s · %(message)s")
        self.settings = settings

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")

    def format(self, record):
        return safe_text(self.settings, super().format(record))


def logging_config(settings):
    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    config["disable_existing_loggers"] = False
    config["formatters"]["purrai"] = {"()": lambda: SafeFormatter(settings)}
    config["handlers"]["purrai"] = {"class": "logging.StreamHandler", "formatter": "purrai", "stream": "ext://sys.stderr"}
    config["loggers"]["purrai"] = {"handlers": ["purrai"], "level": "INFO", "propagate": False}
    return config


def safe_text(settings, value):
    result = str(value)
    for name in ("bot_token", "chat_api_key", "oidc_client_secret", "session_secret"):
        secret = getattr(settings, name, "")
        if secret:
            result = result.replace(secret, "[redacted]")
    result = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", result)
    result = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", result)
    result = re.sub(
        r"(?i)((?:token|api[_-]?key|secret|password|authorization|cookie)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]", result,
    )
    return result


def failure(logger, settings, exc, operation, identity="", *, terminal=False):
    chain, current = [], exc
    while current is not None and len(chain) < 4 and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__
    reasons = []
    for item in chain:
        source = item.orig if isinstance(item, SQLAlchemyError) and getattr(item, "orig", None) else item
        reasons.append(f"{type(item).__name__}: {safe_text(settings, source).replace(chr(10), ' ')}")
    reason = " <- ".join(reasons)[:650]
    frames = [frame for item in reversed(chain) for frame in traceback.extract_tb(item.__traceback__)]
    location = " -> ".join(f"{frame.filename}:{frame.lineno} {frame.name}" for frame in frames)
    summary = f"{operation} {identity} · {reason}"
    log = logger.error if terminal else logger.warning
    log("%s\n%s", summary, safe_text(settings, location))
    return summary[:900]
