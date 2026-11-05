"""
🤖 Daily brief — the day's facts, delivered INTO today's daily note.

📡 Radar (06:30) is the full always-rewritten surface; this job (06:40)
distills the near-horizon slice of the same sources into ONE machine-owned
blockquote block prepended to TODAY's daily note in Life — so the page the
operator opens every morning already carries the day's brief without a
detour through Radar. Pull-first: no email (the radar digest already covers
notable pushes).

Write discipline (the human owns the rest of the page):
  - the brief is a single blockquote block tagged custom-hermes="daily-brief"
  - re-runs update that block in place (state file, attr-recovery via SQL)
  - nothing else in the daily note is ever read, moved, or rewritten
  - no H1 anywhere (SiYuan doc titles live outside the body)

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.daily_brief
"""

import json
import os
import sys
from datetime import datetime, timedelta

from loguru import logger

LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
STATE_FILE = os.getenv(
    "DAILY_BRIEF_STATE_FILE", "/opt/iiab/ai-gateway/data/daily_brief_state.json"
)
RADAR_PATH = "/📡 Radar"
BRIEF_ATTR = "daily-brief"
TALK_HORIZON_DAYS = 10
KEY_DATE_HORIZON_DAYS = 7
EVENT_HORIZON_DAYS = 2


def _die(msg: str) -> None:
    logger.error(f"Daily brief ABORTED: {msg}")
    sys.exit(1)


def _load_state() -> dict:
    try:
        return json.loads(open(STATE_FILE).read())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        open(STATE_FILE, "w").write(json.dumps(state))
    except OSError as e:
        logger.warning(f"Daily brief: cannot persist state: {e}")


def _radar_doc_id() -> str | None:
    from app.hooks.siyuan_tools import _siyuan_request, get_notebook_id
    nb_id = get_notebook_id(LIFE_NOTEBOOK)
    if not nb_id:
        return None
    ids = _siyuan_request(
        "filetree/getIDsByHPath",
        {"notebook": nb_id, "path": RADAR_PATH}, quiet=True,
    ).get("data") or []
    return ids[0] if ids else None


def _render(now: datetime) -> tuple[list[str], bool]:
    """Today's near-horizon facts + your open loops, as content lines (no
    SiYuan-only tail, no quoting). Returns (lines, meaningful) — meaningful is
    False when there's nothing worth a push (no horizon facts, no open loops),
    so a quiet morning doesn't ping you."""
    from app.jobs.cockpit_radar import (
        _countdown, _key_dates, _prep_state, _upcoming_events, _upcoming_talks,
    )

    from app.graph.tools.capture import local_today, open_items, to_local
    speaker = os.getenv("TALK_SYNC_SPEAKER", "").strip().lower()
    today = local_today(now)          # operator-local calendar day, not the box's
    lines = [f"🤖 **Auto brief** — {to_local(now):%Y-%m-%d %H:%M}", ""]
    meaningful = False

    talk_lines = []
    for t in _upcoming_talks():
        who = t.get("speaker") or "?"
        when = t.get("week_of", "?")
        no = t.get("talk_number") or "—"
        try:
            # week_of is the week's MONDAY; the public talk is that Sunday.
            delivery = datetime.strptime(when, "%Y/%m/%d") + timedelta(days=6)
        except ValueError:
            continue
        days = (delivery - today).days
        if days < 0 or days > TALK_HORIZON_DAYS:
            continue
        mine = speaker and speaker in who.lower()
        flag = " ← **YOU**" if mine else ""
        talk_lines.append(
            f"🎤 {delivery:%Y-%m-%d} · No {no} · {who}{flag} — {_countdown(days)}"
        )
    if talk_lines:
        meaningful = True
    lines += talk_lines or ["🎤 *(no talks on the horizon)*"]

    key_dates, _ = _key_dates(now)
    for dt, label in key_dates:
        days = (dt - today).days
        if days <= KEY_DATE_HORIZON_DAYS:
            lines.append(f"⭐ {dt:%Y-%m-%d} · {label} — {_countdown(days)}")
            meaningful = True

    for ev in _upcoming_events()[:6]:
        start = str(ev.get("start") or ev.get("date") or "")[:10]
        name = ev.get("name") or ev.get("title", "?")
        try:
            days = (datetime.fromisoformat(start) - today).days
        except ValueError:
            continue
        if 0 <= days <= EVENT_HORIZON_DAYS:
            lines.append(f"📅 {start} · {name} — {_countdown(days)}")
            meaningful = True

    # 🎯 Open loops — YOUR still-open tickets/incidents (from captures), so the
    # morning message accounts for what you've jotted, not just the calendar.
    loops = open_items()
    if loops:
        meaningful = True
        lines += ["", f"🎯 **Open loops** ({len(loops)}):"]
        for r in list(reversed(loops))[:6]:
            tag = "🔴" if r.get("kind") == "incident" else "🎫"
            lines.append(f"{tag} `[{r.get('id', '?')}]` {r.get('text', '')}")

    prep = _prep_state()
    if prep:
        lines.append("")
        lines += [f"🗣️ {p}" for p in prep[:3]]

    return lines, meaningful


def _find_existing_block(doc_id: str, state: dict, today_key: str) -> str | None:
    """Today's brief block if it already exists: state file first (kernel
    read, immune to SQL-index lag), then attr-recovery via SQL."""
    from app.hooks.siyuan_tools import _siyuan_request, siyuan_get_attrs

    if state.get("date") == today_key and state.get("block_id"):
        attrs = siyuan_get_attrs(state["block_id"])
        if attrs:
            return state["block_id"]
    stmt = (
        "SELECT id FROM blocks WHERE root_id='{doc}' "
        "AND ial LIKE '%custom-hermes=\"{attr}\"%' LIMIT 1"
    ).format(doc=doc_id, attr=BRIEF_ATTR)
    rows = _siyuan_request("query/sql", {"stmt": stmt}, quiet=True).get("data") or []
    return rows[0]["id"] if rows else None


def main() -> None:
    from app.hooks.siyuan_tools import (
        get_notebook_id, siyuan_create_daily_note, siyuan_prepend_block,
        siyuan_set_attrs, siyuan_update_block,
    )

    now = datetime.now()
    today_key = f"{now:%Y-%m-%d}"

    if not get_notebook_id(LIFE_NOTEBOOK):
        _die(f"SiYuan notebook '{LIFE_NOTEBOOK}' unreachable")

    doc_id = siyuan_create_daily_note(LIFE_NOTEBOOK)
    if not doc_id:
        _die("could not resolve today's daily note")

    lines, meaningful = _render(now)

    # SiYuan block carries the radar block-ref (only renders in SiYuan); the
    # push body omits it. One container block: every line quoted → safe in-place
    # updateBlock.
    siyuan_lines = list(lines)
    radar_id = _radar_doc_id()
    if radar_id:
        siyuan_lines += ["", f'👉 Full detail: (({radar_id} "📡 Radar"))']
    md = "\n".join(f"> {ln}" if ln else ">" for ln in siyuan_lines)

    state = _load_state()
    block_id = _find_existing_block(doc_id, state, today_key)
    if block_id:
        if not siyuan_update_block(block_id, md):
            _die(f"could not update brief block {block_id}")
        action = "updated"
    else:
        block_id = siyuan_prepend_block(doc_id, md)
        if not block_id:
            _die("could not prepend brief block to the daily note")
        action = "created"

    siyuan_set_attrs(block_id, {
        "custom-hermes": BRIEF_ATTR,
        "custom-type": "auto",
        "custom-updated": now.isoformat(timespec="seconds"),
    })
    _save_state({"date": today_key, "doc_id": doc_id, "block_id": block_id})
    logger.info(f"Daily brief OK: doc={doc_id}, block={block_id} ({action})")

    # Push the morning brief (day ahead + open loops) to a channel you receive.
    # Only when there's something worth saying; best-effort, never fatal.
    from app.graph.tools.capture import to_local
    from app.graph.tools.settings import get_setting
    mode = (get_setting("daily_brief_push") or "notify").strip().lower()
    if mode != "off" and meaningful:
        from app.graph.tools.email import notify_admin
        subject = f"🤖 Morning brief — {to_local(now):%a %d %b}"
        body = "\n".join(lines)
        targets = (["email", "whatsapp"] if mode == "both"
                   else [None] if mode == "notify" else [mode])
        for ch in targets:
            if not notify_admin(subject, body, channel=ch):
                logger.warning(f"Daily brief: push to "
                               f"{ch or 'notify_channel'} failed")


if __name__ == "__main__":
    main()
