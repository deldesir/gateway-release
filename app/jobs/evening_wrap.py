"""
🌙 Evening wrap — close the day, tee up tomorrow. Deterministic, zero-token.

At ~20:30 (America/Port-au-Prince) this reads the day's captures (from
app.graph.tools.capture's captures.jsonl — the structured source of truth)
and writes ONE machine-owned blockquote (custom-hermes="evening-wrap") near
the end of TODAY's daily note in Life:

  - what you captured today (tickets / notes / ministry)
  - this week's open work tickets & incidents (the carry-over)
  - a deterministic "3 priorities for tomorrow" seed (incidents first, then
    the oldest open tickets) — the human overwrites these
  - ministry hours this month (pioneer pace, at a glance)
  - a gentle hard-stop cue: leave work, pray, be present

No LLM: the value is mechanical (surface what you logged, carry the open
items). Same containment discipline as daily_synthesis: run-once-per-day
state guard (EVENING_FORCE=1 regenerates in place), the job owns the write,
loud non-zero exit → OnFailure notifies the operator.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.evening_wrap
"""

import calendar
import json
import os
import sys
from datetime import datetime, timedelta

from loguru import logger

LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
# Tunables (ministry goal, secretary nudges, aging threshold, energy streak,
# full/compact detail) are read live from the operator's settings store — see
# app.graph.tools.settings; tune them from chat, no restart.
STATE_FILE = os.getenv(
    "EVENING_WRAP_STATE_FILE",
    "/opt/iiab/ai-gateway/data/evening_wrap_state.json",
)
SYNTH_STATE_FILE = os.getenv(
    "DAILY_SYNTHESIS_STATE_FILE",
    "/opt/iiab/ai-gateway/data/daily_synthesis_state.json",
)
BRIEF_STATE_FILE = os.getenv(
    "DAILY_BRIEF_STATE_FILE", "/opt/iiab/ai-gateway/data/daily_brief_state.json"
)
WRAP_ATTR = "evening-wrap"


def _die(msg: str) -> None:
    logger.error(f"Evening wrap ABORTED: {msg}")
    sys.exit(1)


def _load_json(path: str) -> dict:
    try:
        return json.loads(open(path).read())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        open(STATE_FILE, "w").write(json.dumps(state))
    except OSError as e:
        logger.warning(f"Evening wrap: cannot persist state: {e}")


def _week_start(now: datetime) -> datetime:
    """Monday 00:00 of the OPERATOR's local week, returned as a box-naive
    instant so it lines up with the box-stamped capture timestamps."""
    from app.graph.tools.capture import local_day_start, to_local
    lnow = to_local(now)
    monday_local = lnow - timedelta(days=lnow.weekday())
    return local_day_start(monday_local)


def _age_days(rec: dict, now: datetime) -> int | None:
    try:
        return (now - datetime.fromisoformat(rec["ts"])).days
    except (KeyError, ValueError):
        return None


def _energy_read(now: datetime) -> str | None:
    """Watch the human, not the tasks: count the CURRENT run of consecutive
    days that logged work but nothing for family/study/ministry. A gentle
    forward nudge when that run is long — never a scold, and a missing
    capture is not proof of neglect (it only fires on a real streak)."""
    from app.graph.tools.capture import local_day_start, read_captures
    from app.graph.tools.settings import get_setting
    work = {"ticket", "incident", "business", "close"}
    care = {"family", "study", "ministry"}
    streak = 0
    base = local_day_start(now)         # operator-local day buckets, not the box's
    for i in range(14):
        day = base - timedelta(days=i)
        kinds = {r.get("kind") for r in
                 read_captures(since=day, until=day + timedelta(days=1))}
        if kinds & care:
            break                      # a care day ends the run
        if kinds & work:
            streak += 1
        else:
            break                      # a blank day = no signal, stop
    if streak >= get_setting("energy_streak_days"):
        return (f"🌿 {streak} days straight of all-work captures — nothing logged "
                f"for rest, family, or ministry. Tomorrow, protect a little for "
                f"what comes first.")
    return None


def _render(now: datetime) -> str:
    from app.graph.tools.capture import (
        local_day_start, month_ministry_hours, open_items, read_captures, to_local,
    )
    from app.graph.tools.settings import get_setting, is_snoozed

    stale_days = get_setting("ticket_stale_days")
    compact = get_setting("evening_wrap") == "compact"

    # "today" = the operator's LOCAL calendar day, not the box's. The wrap fires
    # at 20:30 Haiti = 02:30 box, so a box-midnight boundary would have already
    # rolled to tomorrow and miss every capture from the day just lived.
    today = local_day_start(now)
    lnow = to_local(now)               # operator-local wall clock, for month/day pace
    todays = read_captures(since=today)
    by = {}
    hours_today = 0.0
    for r in todays:
        by.setdefault(r.get("kind"), []).append(r)
        if r.get("kind") == "ministry":
            hours_today += float(r.get("hours") or 0)

    # this week's still-OPEN tickets/incidents (closed ones excluded)
    week_tix = open_items(since=_week_start(now))
    week_tix_new = list(reversed(week_tix))  # newest first for display
    incidents = [r for r in week_tix if r.get("kind") == "incident"]
    tickets = [r for r in week_tix if r.get("kind") == "ticket"]

    # loop-closing: anything still open past the stale threshold (i.e. older
    # than this week) — the dropped balls a this-week view would never show.
    # Muteable from chat ("mute aging nudges until …").
    aging = [] if is_snoozed("aging", now) else sorted(
        (r for r in open_items()
         if (_age_days(r, now) or 0) >= stale_days),
        key=lambda r: _age_days(r, now) or 0, reverse=True,
    )

    lines = [f"🌙 **Evening wrap** — {to_local(now):%Y-%m-%d %H:%M}", ""]

    counts = []
    for k, icon in (("ticket", "🎫"), ("incident", "🔴"),
                    ("note", "📝"), ("person", "👤"), ("business", "🛒"),
                    ("family", "❤️"), ("study", "📖"), ("reminder", "⏰")):
        if by.get(k):
            counts.append(f"{icon} {len(by[k])} {k}")
    if hours_today:
        counts.append(f"⛪ {round(hours_today, 2)}h ministry")
    lines.append("**Captured today:** " + (" · ".join(counts) if counts else "nothing"))
    lines.append("")

    # Adoption signal: total silence is itself worth surfacing — the wrap can
    # only reflect what you feed it. Gentle, and only when genuinely idle for a
    # week (the energy read can't fire without work captures to read).
    if not is_snoozed("capture_idle", now):
        from app.graph.tools.capture import capture_activity
        if capture_activity(now, days=7)["total"] == 0:
            lines.append("💤 Nothing captured in 7 days — the cockpit only "
                         "reflects what you jot. A `note:` or `person:` is a start.")
            lines.append("")

    if week_tix_new:
        lines.append("**Open tickets/incidents (this week):**")
        for r in week_tix_new[:8]:
            tag = "🔴" if r.get("kind") == "incident" else "🎫"
            lines.append(f"- {tag} `[{r.get('id', '?')}]` {r.get('text', '')}")
        lines.append("")

    if aging:
        if compact:
            lines.append(f"**⏳ Aging:** {len(aging)} open over a week — "
                         "`close:` when done.")
            lines.append("")
        else:
            lines.append("**⏳ Aging — close or act (open over a week):**")
            for r in aging[:6]:
                tag = "🔴" if r.get("kind") == "incident" else "🎫"
                lines.append(f"- {tag} `[{r.get('id', '?')}]` {r.get('text', '')} · "
                             f"**{_age_days(r, now)}d** — `close: {r.get('id', '?')}`?")
            lines.append("")

    # deterministic 3-priority seed: incidents first, then oldest tickets.
    # Compact wrap drops the seed (it's the fuller-detail affordance).
    if not compact:
        seed = incidents + tickets
        lines.append("**Tomorrow's 3 priorities:** *(edit as you like)*")
        for i in range(3):
            if i < len(seed):
                lines.append(f"{i + 1}. {seed[i].get('text', '')}")
            else:
                lines.append(f"{i + 1}. …")
        lines.append("")

    mo = month_ministry_hours(lnow.year, lnow.month)
    last_day = calendar.monthrange(lnow.year, lnow.month)[1]
    days_left = last_day - lnow.day
    goal = _goal_hours()
    if goal:
        remaining = max(0.0, goal - mo)
        if remaining <= 0:
            pace = " — **goal met** 🎉"
        else:
            per_day = remaining / days_left if days_left > 0 else remaining
            pace = (f" — {remaining:.0f}h to go, {days_left}d left"
                    f" (~{per_day:.1f}h/day)")
        lines.append(f"**Ministry this month:** {mo}h / {goal:.0f}h goal{pace}")
    else:
        lines.append(f"**Ministry this month:** {mo}h")

    # Secretary: month-end nudge to gather field-service reports (opt-in).
    if get_setting("secretary_reports") and days_left <= 1:
        lines.append("")
        lines.append("📋 **Secretary:** month ends — collect field-service "
                     "reports from the publishers.")

    energy = None if is_snoozed("energy", now) else _energy_read(now)
    if energy:
        lines.append("")
        lines.append(energy)

    ict = _ict_block(now)
    if ict:
        lines.append("")
        lines.extend(ict)

    lines.append("")
    lines.append("🛑 Now leave work. Pray, and be present with your wife.")
    return "\n".join(lines)


def _ict_block(now: datetime) -> list[str]:
    """Colleagues' requests (RapidPro tickets) since the start of the day. Best effort."""
    try:
        from app.graph.tools.capture import local_day_start
        from app.graph.tools.ict_requests import figures_lines, ticket_figures

        fig = ticket_figures(local_day_start(now), now)
        if not (fig["opened"] or fig["closed"] or fig["open"]):
            return []
        return ["**ICT requests today:** " + figures_lines(fig)[0]] + figures_lines(fig)[1:]
    except Exception as exc:  # never let a report fail over its optional section
        return [f"**ICT requests:** unavailable ({exc})"]


def _goal_hours() -> float | None:
    from app.graph.tools.settings import get_setting
    goal = get_setting("ministry_goal_hours")   # already typed (number) or None
    return goal if goal else None


def _find_block_by_attr(doc_id: str, attr: str) -> str | None:
    """Idempotency fallback: locate a machine-owned block by its custom-hermes
    attribute inside a doc, so a lost/corrupt state file can't spawn a second
    evening-wrap block in the same daily note."""
    from app.hooks.siyuan_tools import _siyuan_request
    stmt = (
        f"SELECT id FROM blocks WHERE root_id='{doc_id}' "
        f"AND ial LIKE '%custom-hermes=\"{attr}\"%' LIMIT 1"
    )
    rows = _siyuan_request("query/sql", {"stmt": stmt}, quiet=True).get("data") or []
    return rows[0]["id"] if rows else None


def main() -> None:
    now = datetime.now()
    today_key = f"{now:%Y-%m-%d}"
    force = os.getenv("EVENING_FORCE", "") == "1"

    from app.hooks.siyuan_tools import (
        get_notebook_id, siyuan_create_daily_note, siyuan_get_attrs,
        siyuan_insert_block_after, siyuan_prepend_block, siyuan_set_attrs,
        siyuan_update_block,
    )

    if not get_notebook_id(LIFE_NOTEBOOK):
        _die(f"SiYuan notebook '{LIFE_NOTEBOOK}' unreachable")

    doc_id = siyuan_create_daily_note(LIFE_NOTEBOOK)
    if not doc_id:
        _die("could not resolve today's daily note")

    # Idempotency: trust the state file first, but fall back to finding the
    # existing machine-owned block by attribute — a lost/corrupt state file
    # must NOT create a second evening-wrap block in the same daily note.
    state = _load_json(STATE_FILE)
    existing_block = None
    if state.get("date") == today_key and state.get("block_id") \
            and siyuan_get_attrs(state["block_id"]):
        existing_block = state["block_id"]
    if not existing_block:
        existing_block = _find_block_by_attr(doc_id, WRAP_ATTR)
    if existing_block and not force:
        logger.info("Evening wrap: already written today — skipping "
                    "(EVENING_FORCE=1 to regenerate)")
        return

    body = _render(now)
    md = "\n".join(f"> {ln}" if ln else ">" for ln in body.splitlines())

    if existing_block:
        if not siyuan_update_block(existing_block, md):
            _die(f"could not update evening-wrap block {existing_block}")
        block_id, action = existing_block, "updated"
    else:
        # place after synthesis (else brief) so the note reads brief→synth→wrap
        anchor = None
        for sf in (SYNTH_STATE_FILE, BRIEF_STATE_FILE):
            st = _load_json(sf)
            if st.get("date") == today_key and st.get("block_id"):
                anchor = st["block_id"]
                break
        block_id = None
        if anchor:
            block_id = siyuan_insert_block_after(anchor, md)
        if not block_id:
            block_id = siyuan_prepend_block(doc_id, md)
        if not block_id:
            _die("could not write evening-wrap block to the daily note")
        action = "created"

    siyuan_set_attrs(block_id, {
        "custom-hermes": WRAP_ATTR,
        "custom-type": "auto",
        "custom-updated": now.isoformat(timespec="seconds"),
    })
    _save_state({"date": today_key, "doc_id": doc_id, "block_id": block_id})
    logger.info(f"Evening wrap OK: doc={doc_id}, block={block_id} ({action})")

    # Push it — so the day's captures reach a channel you actually receive, not
    # only the SiYuan note (which you'd have to open). 'notify' follows
    # notify_channel; 'off' keeps the wrap pull-only. Best-effort: a failed push
    # is logged, never fatal (the authoritative artifact is already written).
    from app.graph.tools.capture import to_local
    from app.graph.tools.settings import get_setting
    mode = (get_setting("evening_wrap_push") or "notify").strip().lower()
    if mode != "off":
        from app.graph.tools.email import notify_admin
        subject = f"🌙 Evening wrap — {to_local(now):%a %d %b}"
        targets = (["email", "whatsapp"] if mode == "both"
                   else [None] if mode == "notify" else [mode])
        for ch in targets:
            if not notify_admin(subject, body, channel=ch):
                logger.warning(f"Evening wrap: push to "
                               f"{ch or 'notify_channel'} failed")


if __name__ == "__main__":
    main()
