"""Small bounded in-process limits for public HTTP and private bot controls."""

import time
from collections import OrderedDict


class RateLimit:
    def __init__(self, per_key, total, seconds=60, max_keys=1024):
        self.per_key, self.total, self.seconds, self.max_keys = per_key, total, seconds, max_keys
        self.keys = OrderedDict()
        self.global_window = (0.0, 0)

    def allow(self, key):
        stamp = time.monotonic()
        start, count = self.global_window
        if stamp - start >= self.seconds:
            start, count = stamp, 0
        if count >= self.total:
            return False
        local_start, local_count = self.keys.pop(key, (stamp, 0))
        if stamp - local_start >= self.seconds:
            local_start, local_count = stamp, 0
        self.keys[key] = (local_start, local_count)
        while len(self.keys) > self.max_keys:
            self.keys.popitem(last=False)
        if local_count >= self.per_key:
            return False
        self.keys[key] = local_start, local_count + 1
        self.global_window = start, count + 1
        return True
