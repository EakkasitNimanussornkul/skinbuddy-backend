"""A simple per-user rate limit, kept in memory.

Used for POST /submissions/images (20 uploads an hour per user). In memory is
enough for this app, which runs as one server process. Two things follow from
that, and both are accepted:
  * the counts reset when the server restarts;
  * with several worker processes, each would keep its own counts, so the real
    limit would be the limit times the number of workers.
A shared store (the database, or Redis) would be needed to do better.
"""

import math
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional


class SlidingWindowLimiter:
    """At most `limit` hits per key in any `window_seconds`-long window."""

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> Optional[int]:
        """Record one hit for key and return None, or, when key has used up
        the limit, record nothing and return the whole seconds until it may try
        again (for a Retry-After header)."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1, math.ceil(hits[0] + self.window - now))
            hits.append(now)
            # Drop keys with nothing recent, so the dict does not grow without bound.
            if len(self._hits) > 10_000:
                for stale in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
                    del self._hits[stale]
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
