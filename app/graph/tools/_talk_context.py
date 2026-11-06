"""Server-side "active talk / active revision" memory, keyed by the person.

TalkPrep is a multi-step path (choose a talk, create a version, draft, challenge…), and which talk
a person is on has to survive between messages on every surface. The store is one JSON file, one
entry per person (phone digits), written atomically; it lives in the engine (talkmaster.context)
so the command line reads and writes the same file as chat, menu and mail. The X-Active-* headers
still win when present (an explicit override); they are no longer required.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from talkmaster.context import ContextStore
except ImportError:          # a box without the talk-prep engine (a shop's retail profile): no active-talk memory
    ContextStore = None


class _NoStore:
    """The store on a box without the engine: remembers nothing, refuses nothing."""

    @staticmethod
    def key(user_id):
        return str(user_id or "")

    def get(self, user_id):
        return {"talk_id": None, "revision_name": None, "pub": None, "section": None}

    def __getattr__(self, name):          # set_talk, set_revision, set_pub, set_section, clear
        return lambda *a, **k: None


_STORE = os.path.abspath(os.getenv(
    "TALKPREP_CONTEXT_FILE",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "state", "talkprep_context.json"),
))
_store = ContextStore(_STORE) if ContextStore else _NoStore()


def _key(user_id: Optional[str]) -> str:
    return (ContextStore or _NoStore).key(user_id)


def get(user_id: Optional[str]) -> dict:
    """Return {'talk_id', 'revision_name', 'pub', 'section'} for a person (values may be None)."""
    return _store.get(user_id)


def set_talk(user_id: Optional[str], talk_id: int) -> None:
    """Remember the person's active talk. Switching talks forgets the active revision."""
    _store.set_talk(user_id, talk_id)


def set_revision(user_id: Optional[str], revision_name: str) -> None:
    _store.set_revision(user_id, revision_name)


def set_pub(user_id: Optional[str], pub: str) -> None:
    _store.set_pub(user_id, pub)


def set_section(user_id: Optional[str], section: str) -> None:
    """The part last worked on: the next `answer`, `draft` or `model` may leave it unnamed."""
    _store.set_section(user_id, section)


def active_section(args: dict) -> Optional[str]:
    """The part named in the call, else the remembered one; '-' and '_' mean 'the remembered one'."""
    val = str(args.get("section_title") or args.get("section") or "").strip()
    if val and val not in ("-", "_"):
        return val
    return get(args.get("user_id")).get("section")


def clear(user_id: Optional[str]) -> None:
    _store.clear(user_id)


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
