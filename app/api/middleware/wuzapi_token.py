"""Resolve the WuzAPI user token of a WhatsApp channel.

RapidPro writes the token into the WZ channel's config when the channel is claimed,
so that row is the source of truth. A copy in the environment goes stale the moment
the channel is re-claimed: every claim creates a new WuzAPI user with a new token,
and every call that carries the old one is refused with 401.

Resolution order:
  1. ``WUZAPI_TOKEN`` in the environment — an explicit override, for deployments
     that do not share RapidPro's database.
  2. The WZ channel named by the caller (``channel=``: a UUID or a number), for a
     feature that sends from its own line — the contact directory does.
  3. ``WUZAPI_CHANNEL`` in the environment, the channel the assistant sends from.
  4. The newest active WZ channel in the shared ``temba`` database.

The database value is cached per channel for ``WUZAPI_TOKEN_TTL_SECS`` (default
300 s); a 401 from WuzAPI invalidates the cache so the next call re-reads the row.
"""

import asyncio
import logging
import os
import threading
import time

logger = logging.getLogger("ai-gateway.wuzapi")

_TTL_SECS = float(os.getenv("WUZAPI_TOKEN_TTL_SECS", "300"))

# With several WhatsApp channels, WUZAPI_CHANNEL (a channel's UUID or number)
# pins the one the gateway sends from; unset, it is the newest active one.
_PINNED = os.getenv("WUZAPI_CHANNEL", "").strip()
_BASE_SQL = (
    "SELECT config->>'wuzapi_token' FROM channels_channel "
    "WHERE channel_type = 'WZ' AND is_active AND config->>'wuzapi_token' IS NOT NULL "
)


def _query(pin: str) -> tuple[str, tuple]:
    """The lookup for one channel (UUID or number, with or without '+'), or the newest."""
    pin = (pin or "").strip()
    sql = _BASE_SQL + ("AND (uuid::text = %s OR address = %s OR address = %s) " if pin else "") + "ORDER BY id DESC LIMIT 1"
    return sql, ((pin, pin, "+" + pin.lstrip("+")) if pin else ())


_SQL, _SQL_PARAMS = _query(_PINNED)

_cache: dict[str, dict] = {}     # channel pin ("" = the default) -> {"token", "at"}
_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        logger.warning(msg)


def _env_token() -> str:
    return os.getenv("WUZAPI_TOKEN", "").strip()


def _pin(channel: str) -> str:
    return (channel or "").strip() or _PINNED


def _cached(channel: str = "") -> str:
    entry = _cache.get(_pin(channel)) or {}
    if entry.get("token") and time.monotonic() - entry.get("at", 0.0) < _TTL_SECS:
        return entry["token"]
    return ""


def _remember(token: str, channel: str = "") -> str:
    _cache[_pin(channel)] = {"token": token, "at": time.monotonic()}
    if not token:
        which = f"channel {_pin(channel)!r}" if _pin(channel) else "any active WhatsApp (WZ) channel"
        _warn_once(f"no wuzapi_token in the database for {which}")
    return token


def invalidate_wuzapi_token() -> None:
    """Forget every cached token (call after a 401 from WuzAPI)."""
    _cache.clear()


async def _lookup_async(channel: str = "") -> str:
    """Run the lookup off the event loop.

    Deliberately not the gateway's shared async engine: its connection pool binds to
    the first event loop that uses it, and the WhatsApp helpers are also called from
    worker threads that run their own loop (tools use ``asyncio.run``), where a pooled
    connection fails with "attached to a different loop". The lookup is rare (once per
    TTL, or after a 401), so a short-lived DB-API connection in a thread is the right
    tool from every loop.
    """
    return await asyncio.to_thread(_lookup_sync, channel)


def _lookup_sync(channel: str = "") -> str:
    """The query over a plain DB-API connection (works from any thread or loop)."""
    uri = os.getenv("POSTGRES_URI", "")
    if not uri.startswith("postgresql"):
        return ""
    sql, params = _query(_pin(channel))
    try:
        import psycopg2

        dsn = uri.split("://", 1)[1]
        conn = psycopg2.connect("postgresql://" + dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        _warn_once(f"cannot read the WhatsApp channel token from the database: {e}")
        return ""
    return (row[0] or "").strip() if row else ""


async def get_wuzapi_token(force_refresh: bool = False, channel: str = "") -> str:
    """The token to send as ``Authorization`` to WuzAPI, or "" when none is known."""
    env = _env_token()
    if env:
        return env
    if not force_refresh:
        cached = _cached(channel)
        if cached:
            return cached
    return _remember(await _lookup_async(channel), channel)


def get_wuzapi_token_sync(force_refresh: bool = False, channel: str = "") -> str:
    """Synchronous twin of :func:`get_wuzapi_token` for non-async callers."""
    env = _env_token()
    if env:
        return env
    if not force_refresh:
        cached = _cached(channel)
        if cached:
            return cached
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _remember(_lookup_sync(channel), channel)
    # Called from inside a running loop: never block it with a DB round-trip.
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(token=_lookup_sync(channel)), daemon=True)
    worker.start()
    worker.join()
    return _remember(result.get("token", ""), channel)


def wuzapi_token_available(channel: str = "") -> bool:
    """Whether WhatsApp calls through WuzAPI can be attempted right now."""
    return bool(get_wuzapi_token_sync(channel=channel))
