"""Single-process claims that preserve ordering without holding locks on HTTP."""

import asyncio
from contextlib import asynccontextmanager


class WorkClaims:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.identifiers = set()
        self.scopes = set()
        self.owners = {}

    @asynccontextmanager
    async def selecting(self):
        async with self.lock:
            owners = self.owners.copy()
            try:
                yield
            except BaseException:
                # Selection includes closing its database session. If that
                # handoff fails, no caller owns the reservations made here.
                for identifier, owner in list(self.owners.items()):
                    if owners.get(identifier) != owner:
                        self.release(identifier, owner[0])
                raise

    def available(self, identifier, scope):
        return identifier not in self.identifiers and scope not in self.scopes

    def claim(self, identifier, scope):
        # Selection and claiming happen together under lock. A restart simply
        # reclaims the durable pending work; no persisted running state can stick.
        if not self.available(identifier, scope):
            return False
        self.identifiers.add(identifier)
        self.scopes.add(scope)
        self.owners[identifier] = (scope, object())
        return True

    def release(self, identifier, scope):
        self.identifiers.discard(identifier)
        self.scopes.discard(scope)
        self.owners.pop(identifier, None)
