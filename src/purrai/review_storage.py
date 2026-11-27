"""Payload accounting shared by ORM writes and bounded operational maintenance."""

import json
import time

from sqlalchemy import LargeBinary, case, cast, event, func, inspect, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

BYTES_KEY = "review-storage:bytes"
LIMITED_KEY = "review-storage:limited"


def payload_size(expression):
    return case((func.json_extract(expression, "$._retained") == 1, 0),
                else_=func.length(cast(expression, LargeBinary)))


def retained_message(row):
    message = row.message or {}
    sender = message.get("sender_chat") or message.get("from") or {}
    return {"_retained": True, "message_id": row.message_id, "date": row.event_at,
        "chat": {"id": row.group_id, "type": message.get("chat", {}).get("type", "supergroup")},
        "from": {"id": sender.get("id", row.user_id),
                 "first_name": (sender.get("first_name") or sender.get("title") or "")[:80]},
        "text": (message.get("text") or message.get("caption") or "")[:160]}


def install_accounting(review, meta):
    table, metadata = review.__table__, meta.__table__

    @event.listens_for(Session, "before_flush")
    def account(session, *_):
        rows = [row for row in set(session.new) | set(session.dirty) | set(session.deleted) if isinstance(row, review)]
        rows = [row for row in rows if row in session.new or row in session.deleted or
                inspect(row).attrs.message.history.has_changes()]
        if not rows:
            return
        connection = session.connection()
        saved = connection.execute(select(metadata.c.value).where(metadata.c.key == BYTES_KEY)).scalar()
        total = int(saved) if saved is not None else int(connection.execute(
            select(func.coalesce(func.sum(payload_size(table.c.message)), 0))).scalar())
        settings = getattr(session.info.get("database"), "settings", None)
        limit = getattr(settings, "review_payload_max_mb", 64) * 1048576
        limited = 0
        for row in rows:
            old = 0 if row in session.new else int(connection.execute(select(payload_size(table.c.message))
                .where(table.c.id == row.id)).scalar() or 0)
            if row in session.deleted:
                total -= old
                continue
            size = 0 if row.message.get("_retained") else len(json.dumps(row.message).encode())
            if size > old and total + size - old > limit:
                row.message = retained_message(row)
                size = 0
                if row.state in (None, "pending", "requesting"):
                    row.state = "cancelled"
                    row.error = "审核暂存容量已满，本条未送入模型；群规则仍正常检查"
                    limited += 1
            total += size - old
        def save(key, value):
            statement = insert(metadata).values(key=key, value=value, expires_at=0)
            connection.execute(statement.on_conflict_do_update(index_elements=[metadata.c.key], set_={"value": value}))
        save(BYTES_KEY, str(max(0, total)))
        if limited:
            stored = connection.execute(select(metadata.c.value).where(metadata.c.key == LIMITED_KEY)).scalar()
            previous = json.loads(stored) if stored else {}
            save(LIMITED_KEY, json.dumps({"at": time.time(), "count": previous.get("count", 0) + limited}))
