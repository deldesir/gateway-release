"""
Circuit breaker for Hermes AI invocations.

Replaces the old sticky ``noai`` RiveBot variable with an in-memory
breaker that **auto-recovers** after a cooldown period.

States:
    CLOSED    → Normal.  Every request reaches Hermes.
    OPEN      → AI down. Requests are short-circuited with a degraded msg.
    HALF_OPEN → Cooldown expired.  ONE probe request is allowed through.
                If it succeeds → CLOSED.  If it fails → OPEN again.

No persistence — a gateway restart resets to CLOSED (clean slate).
"""

import time
import threading
from enum import Enum
from typing import Optional

from app.logger import logger

_logger = logger.bind(name="CircuitBreaker")


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ── Tunables ─────────────────────────────────────────────────────────────────
FAILURE_THRESHOLD: int = 3       # consecutive failures before opening
FAILURE_WINDOW: float = 120.0    # seconds — rolling window for counting
COOLDOWN: float = 60.0           # seconds before OPEN → HALF_OPEN

# ── Internal state (module-level singleton) ──────────────────────────────────
_lock = threading.Lock()
_state: State = State.CLOSED
_failure_timestamps: list[float] = []
_opened_at: float = 0.0
_last_failure_reason: Optional[str] = None


def _prune_old_failures() -> None:
    """Remove failure timestamps outside the rolling window."""
    global _failure_timestamps
    cutoff = time.time() - FAILURE_WINDOW
    _failure_timestamps = [t for t in _failure_timestamps if t > cutoff]


def can_attempt() -> bool:
    """Should we attempt an AI call right now?

    Returns True  → proceed to Hermes
    Returns False → return degraded message immediately
    """
    global _state, _opened_at

    with _lock:
        if _state == State.CLOSED:
            return True

        if _state == State.OPEN:
            elapsed = time.time() - _opened_at
            if elapsed >= COOLDOWN:
                _state = State.HALF_OPEN
                _logger.info(
                    f"Cooldown expired ({elapsed:.0f}s) — transitioning to HALF_OPEN"
                )
                return True  # allow ONE probe
            return False

        # HALF_OPEN — only one probe in flight; block others
        # (simplification: we allow all requests in half_open;
        #  the first success/failure will transition the state)
        return True


def record_success() -> None:
    """AI call succeeded — close the circuit."""
    global _state, _failure_timestamps, _last_failure_reason

    with _lock:
        prev = _state
        _state = State.CLOSED
        _failure_timestamps.clear()
        _last_failure_reason = None
        if prev != State.CLOSED:
            _logger.info(f"AI recovered — circuit {prev.value} → CLOSED")


def record_failure(reason: str = "") -> None:
    """AI call failed — maybe open the circuit."""
    global _state, _opened_at, _last_failure_reason

    with _lock:
        now = time.time()
        _failure_timestamps.append(now)
        _prune_old_failures()
        _last_failure_reason = reason

        if _state == State.HALF_OPEN:
            # Probe failed — reopen
            _state = State.OPEN
            _opened_at = now
            _logger.warning(f"Probe failed — circuit HALF_OPEN → OPEN: {reason}")
            return

        if _state == State.CLOSED:
            if len(_failure_timestamps) >= FAILURE_THRESHOLD:
                _state = State.OPEN
                _opened_at = now
                _logger.warning(
                    f"Threshold reached ({len(_failure_timestamps)} failures "
                    f"in {FAILURE_WINDOW}s) — circuit CLOSED → OPEN: {reason}"
                )


def status() -> dict:
    """Return current breaker state for admin/debug endpoints."""
    with _lock:
        _prune_old_failures()
        info = {
            "state": _state.value,
            "recent_failures": len(_failure_timestamps),
            "threshold": FAILURE_THRESHOLD,
            "window_seconds": FAILURE_WINDOW,
            "cooldown_seconds": COOLDOWN,
        }
        if _state == State.OPEN:
            info["open_for_seconds"] = round(time.time() - _opened_at, 1)
            info["cooldown_remaining"] = round(
                max(0, COOLDOWN - (time.time() - _opened_at)), 1
            )
        if _last_failure_reason:
            info["last_failure"] = _last_failure_reason
        return info
