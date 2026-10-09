"""A 60-second in-process cache for rows that are the same for every caller.

Only user-independent database reads go through here: the product tree, the two
conflict-rule tables and the ingredient list. Nothing about a user (skin type,
shelf, profile) is ever stored, and no key is derived from a user, so one
caller's data cannot reach another. The scoring that depends on the user is
computed per request, from the cached rows.

Cached values are shared between requests, so callers must treat them as
read-only. Nothing in the app mutates the rows it reads from here; the tests pin
that (a second call is not affected by the first).

Writes made by this app that change what these tables show (approving a
submission, PATCH /products/{id}) call clear(). Writes made elsewhere (the SQL
editor, the loader scripts) are bounded only by the TTL. The cache is per
process: with several uvicorn workers a clear() reaches its own worker only, and
the others serve their copy until it expires.
"""

import threading
import time
from typing import Any, Callable, Dict, Hashable, Tuple

TTL_SECONDS = 60.0

_clock = time.monotonic          # tests replace this to move time without sleeping
_lock = threading.Lock()
_entries: Dict[Hashable, Tuple[float, Any]] = {}
_generation = 0                  # bumped by clear(), so a load that began before it is not stored


def get_or_load(key: Hashable, loader: Callable[[], Any]) -> Any:
    """The cached value for `key` if it is under 60 s old, else loader()'s result,
    which is stored. A loader that raises stores nothing."""
    with _lock:
        hit = _entries.get(key)
        if hit and hit[0] > _clock():
            return hit[1]
        started_at = _generation
    value = loader()
    with _lock:
        # A clear() while the loader was reading means this value may predate the
        # write that asked for the clear: hand it to this caller, but do not keep it.
        if started_at == _generation:
            _entries[key] = (_clock() + TTL_SECONDS, value)
    return value


def clear() -> None:
    """Drop everything. Called after a write that changes cached data."""
    global _generation
    with _lock:
        _entries.clear()
        _generation += 1
