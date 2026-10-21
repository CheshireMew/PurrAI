"""Shared dependencies and idempotent audit records for administration."""

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import (
    Action,
    uid,
)


class AdminContext:
    def __init__(self, db, telegram, settings, access):
        self.db, self.tg, self.settings, self.access = db, telegram, settings, access

    async def previous(self, session, actor, request_id):
        if not request_id:
            return None
        row = await session.scalar(select(Action).where(Action.dedupe_key == f"change:{actor}:{request_id}"))
        return row.payload["result"] if row else None

    async def record(self, session, actor, group_id, kind, reason, result, request_id=""):
        row = await enqueue(
            session,
            kind,
            f"change:{actor}:{request_id or uid()}",
            group_id,
            actor_id=actor,
            reason=reason,
            payload={"result": result},
        )
        row.state = "succeeded"
        return result
