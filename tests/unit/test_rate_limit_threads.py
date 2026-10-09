"""The upload rate limiter (app/core/services/rate_limit.py) when many threads hit it at once.

Plain `def` handlers run in worker threads, so several requests can reach one limiter at
the same moment. Its count has to be exact then, or a user could slip past the 20 an hour.
The interpreter is told to switch threads as often as it can, so a missing lock shows.
Docstrings state the expected output and are lifted verbatim into the Test Record.
"""

import sys
import threading
from contextlib import contextmanager

from app.core.services.rate_limit import SlidingWindowLimiter


@contextmanager
def eager_thread_switching():
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def hammer(limiter, keys_per_thread, hits_each, threads=32):
    """Run `threads` threads at once; thread t hits key keys_per_thread(t) hits_each times.
    Returns the number of hits each key was allowed, and any exception a thread raised."""
    allowed, errors, lock = {}, [], threading.Lock()
    start = threading.Barrier(threads)

    def work(t):
        start.wait()
        try:
            for i in range(hits_each):
                key = keys_per_thread(t, i)
                ok = limiter.hit(key) is None
                with lock:
                    allowed[key] = allowed.get(key, 0) + ok
        except Exception as e:      # noqa: BLE001 - reported to the test
            errors.append(repr(e))

    with eager_thread_switching():
        pool = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
        [p.start() for p in pool]
        [p.join() for p in pool]
    return allowed, errors


def test_32_threads_hitting_one_key_are_allowed_exactly_the_limit():
    """Allows exactly 20 of 640 simultaneous hits on one key from 32 threads (20 hits each),
    refuses the other 620, and raises nothing: the count is exact under contention."""
    for _ in range(5):
        allowed, errors = hammer(SlidingWindowLimiter(20, 3600), lambda t, i: "user-1", hits_each=20)
        assert errors == []
        assert allowed == {"user-1": 20}


def test_threads_hitting_different_keys_are_each_allowed_their_own_limit():
    """Allows exactly 20 hits for each of 4 keys when 32 threads share them (8 threads a
    key, 40 hits each), so one user's hits never use up, or add to, another user's."""
    allowed, errors = hammer(SlidingWindowLimiter(20, 3600), lambda t, i: f"user-{t % 4}", hits_each=40)
    assert errors == []
    assert allowed == {f"user-{k}": 20 for k in range(4)}


def test_pruning_stale_keys_while_other_threads_add_keys_does_not_fail():
    """Raises nothing and allows every first hit when 8 threads add 1,500 new keys each
    (12,000 in all, past the 10,000 at which stale keys are pruned): the dictionary is never
    changed while another thread is walking it."""
    now = [0.0]
    limiter = SlidingWindowLimiter(20, 10, clock=lambda: now[0])
    # 10,001 keys that expire at once, so the next hits all have something to prune.
    for n in range(10_001):
        limiter.hit(f"old-{n}")
    now[0] = 100.0
    allowed, errors = hammer(limiter, lambda t, i: f"new-{t}-{i}", hits_each=1500, threads=8)
    assert errors == []
    assert sum(allowed.values()) == 8 * 1500


def test_a_slow_check_cannot_let_two_threads_take_the_last_slot():
    """Allows exactly 5 of 16 simultaneous first hits on one key with a limit of 5, although
    reading the count is made slow (1 ms) to hold the gap between the check and the write open:
    a second thread waits for the first instead of passing the same check."""
    import time
    from collections import deque

    class SlowCount(deque):
        def __len__(self):
            n = super().__len__()
            time.sleep(0.001)
            return n

    limiter = SlidingWindowLimiter(5, 3600)
    limiter._hits["user-1"] = SlowCount()
    allowed, errors = hammer(limiter, lambda t, i: "user-1", hits_each=1, threads=16)
    assert errors == []
    assert allowed == {"user-1": 5}
