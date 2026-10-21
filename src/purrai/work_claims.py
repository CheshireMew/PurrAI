"""Single-process claims that preserve ordering without holding locks on HTTP."""

import asyncio


class WorkClaims:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.identifiers = set()
        self.scopes = set()

    def available(self, identifier, scope):
        return identifier not in self.identifiers and scope not in self.scopes

    def claim(self, identifier, scope):
        # Selection and claiming happen together under lock. A restart simply
        # reclaims the durable pending work; no persisted running state can stick.
        if not self.available(identifier, scope):
            return False
        self.identifiers.add(identifier)
        self.scopes.add(scope)
        return True

    def release(self, identifier, scope):
        self.identifiers.discard(identifier)
        self.scopes.discard(scope)
