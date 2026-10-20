"""Correlated diagnostics without credentials or SQL parameter dumps."""

import re
import traceback

from sqlalchemy.exc import SQLAlchemyError


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
