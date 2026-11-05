"""Server-side "active talk / active revision" memory, keyed by the user.

The problem this solves: TalkPrep is a multi-step workflow (pick a talk →
create a revision → develop sections). Which talk you're on has to survive
between messages. The old design threaded it as `{{set:...}}` sentinels that
only the RiveBot chat path understood — so the WhatsApp *menu* path (RapidPro
flows) never remembered anything and every step failed with "no active talk".

Here the active context is plain server state, keyed by the caller's phone.
Both paths call the same tools, so both share the same memory. The old
X-Active-* headers still win when present (an explicit override), but they are
no longer required for the workflow to hold together.

Storage is a small JSON file (one entry per phone). No schema migration, easy
to inspect or reset, written atomically.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Optional

_STORE = os.getenv(
    "TALKPREP_CONTEXT_FILE",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "state",
                 "talkprep_context.json"),
)
_STORE = os.path.abspath(_STORE)


def _key(user_id: Optional[str]) -> str:
    """Normalize a URN / user id to a stable per-person key (phone digits).

    `whatsapp:+15550104949`, `tel:+15550104949` and `+15550104949` all map to
    the same key so a person keeps one active talk across channels. Non-phone
    ids (e.g. the literal "rivebot") fall back to themselves.
    """
    if not user_id:
        return ""
    digits = re.sub(r"\D", "", user_id)
    return digits or user_id.strip().lower()


def _load() -> dict:
    try:
        with open(_STORE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_STORE), prefix=".ctx-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _STORE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(user_id: Optional[str]) -> dict:
    """Return {'talk_id', 'revision_name', 'pub'} for a user (values may be None)."""
    entry = _load().get(_key(user_id)) or {}
    return {
        "talk_id": entry.get("talk_id"),
        "revision_name": entry.get("revision_name"),
        "pub": entry.get("pub"),
    }


def set_talk(user_id: Optional[str], talk_id: int) -> None:
    """Remember the user's active talk. Clears the stale active revision."""
    k = _key(user_id)
    if not k:
        return
    data = _load()
    entry = data.get(k) or {}
    if entry.get("talk_id") != talk_id:
        entry["revision_name"] = None  # switching talks invalidates the revision
    entry["talk_id"] = int(talk_id)
    data[k] = entry
    _save(data)


def set_revision(user_id: Optional[str], revision_name: str) -> None:
    """Remember the user's active revision."""
    k = _key(user_id)
    if not k or not revision_name:
        return
    data = _load()
    entry = data.get(k) or {}
    entry["revision_name"] = revision_name
    data[k] = entry
    _save(data)


def set_pub(user_id: Optional[str], pub: str) -> None:
    """Remember the user's active publication (for the import flow)."""
    k = _key(user_id)
    if not k or not pub:
        return
    data = _load()
    entry = data.get(k) or {}
    entry["pub"] = pub
    data[k] = entry
    _save(data)


def clear(user_id: Optional[str]) -> None:
    k = _key(user_id)
    data = _load()
    if k in data:
        del data[k]
        _save(data)


def active_talk_id(args: dict) -> Optional[int]:
    """Resolve the active talk id: explicit header/arg first, then stored."""
    raw = args.get("active_talk_id")
    if raw not in (None, "", "undefined"):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return get(args.get("user_id")).get("talk_id")


def active_revision(args: dict) -> Optional[str]:
    """Resolve the active revision: explicit header/arg first, then stored."""
    for key in ("revision_name", "active_revision"):
        val = args.get(key)
        if val not in (None, "", "undefined"):
            return str(val)
    return get(args.get("user_id")).get("revision_name")


def active_pub(args: dict) -> Optional[str]:
    """Resolve the active publication: explicit header/arg first, then stored."""
    for key in ("pub_code", "active_pub"):
        val = args.get(key)
        if val not in (None, "", "undefined"):
            return str(val)
    return get(args.get("user_id")).get("pub")
