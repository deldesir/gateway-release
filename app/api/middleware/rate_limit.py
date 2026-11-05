"""Per-user rate limit for the chat path: a flood from one number must not spend the model's
budget or crowd out everyone else. Two sliding windows per user id, a minute and a day, sized
from the environment:

    GATEWAY_RATE_PER_MINUTE   default 12
    GATEWAY_RATE_PER_DAY      default 400

In-memory and per process: enough for one gateway instance, which is what runs. The windows
are pruned on each call; nothing persists across a restart, which is the right failure mode
(a restart forgives, never punishes).
"""
import os
import threading
import time
from collections import deque

PER_MINUTE = int(os.getenv("GATEWAY_RATE_PER_MINUTE", "12"))
PER_DAY = int(os.getenv("GATEWAY_RATE_PER_DAY", "400"))

_lock = threading.Lock()
_minute: dict = {}
_day: dict = {}


def allow(user_id: str, now: float | None = None) -> bool:
    """True when this request fits both windows; the request is counted only when allowed."""
    if PER_MINUTE <= 0 and PER_DAY <= 0:
        return True
    t = now if now is not None else time.time()
    with _lock:
        m = _minute.setdefault(user_id, deque())
        d = _day.setdefault(user_id, deque())
        while m and t - m[0] >= 60:
            m.popleft()
        while d and t - d[0] >= 86400:
            d.popleft()
        if (PER_MINUTE > 0 and len(m) >= PER_MINUTE) or (PER_DAY > 0 and len(d) >= PER_DAY):
            return False
        m.append(t)
        d.append(t)
        return True


def reset() -> None:
    """Forget every window (tests)."""
    with _lock:
        _minute.clear()
        _day.clear()
