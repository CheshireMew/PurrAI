"""One action, short SQLite stages, and checked handoffs to external I/O."""

import asyncio
import copy

from sqlalchemy import inspect

from purrai.models import Action, Group, Member, Meta, Verification


class ActionSuperseded(Exception):
    def __init__(self, result=None, *, reason="成员批次或管理决定已变化，已停止旧任务", before_request=False):
        super().__init__(reason)
        self.result = result
        self.before_request = before_request


class ActionPreflightError(ConnectionError):
    """A retryable check failed before the Telegram effect was attempted."""


class ActionExecution:
    def __init__(self, db):
        self.db = db
        self.session = db.sessions()
        self.locked = False

    async def __aenter__(self):
        await self.db.lock.acquire()
        self.locked = True
        self.session.info["action_execution"] = self
        return self.session

    async def __aexit__(self, kind, exc, tb):
        try:
            if kind is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()
            if self.locked:
                self.db.lock.release()
                self.locked = False

    @staticmethod
    def identity(row):
        if isinstance(row, Group):
            return row.approved
        if isinstance(row, Action):
            return row.state, row.kind
        if isinstance(row, Verification):
            return row.state, row.nonce, row.generation
        if isinstance(row, Member):
            return row.generation, row.present, row.restriction_conflict, copy.deepcopy(row.owned_restriction)
        if isinstance(row, Meta) and row.key.startswith("member-decision:"):
            return row.value

    async def external(self, operation):
        # Commit the intent (including restriction ownership) before the request.
        # Releasing only the asyncio lock would leave a SQLite write transaction open.
        await self.session.flush()
        rows = list(self.session.identity_map.values())
        baseline = [(row, self.identity(row)) for row in rows if self.identity(row) is not None]
        await self.session.commit()
        self.db.lock.release()
        self.locked = False
        result, error = None, None
        try:
            result = await operation()
        except BaseException as exc:
            error = exc
        finally:
            await self.db.lock.acquire()
            self.locked = True
        if isinstance(error, asyncio.CancelledError):
            raise error
        deleted = set()
        for row in rows:
            if await self.session.get(type(row), inspect(row).identity, populate_existing=True) is None:
                deleted.add(id(row))
                if row in self.session:
                    self.session.expunge(row)
        if any(id(row) in deleted or self.identity(row) != value for row, value in baseline):
            raise ActionSuperseded(result) from error
        if error is not None:
            raise error
        return result
