"""
🏠 Cockpit Home — the one screen. Deterministic, zero-token, pull-only.

The cockpit grew many surfaces (Radar, Week Ahead, Work, daily brief,
captures). This job consolidates the near-horizon of all of them into ONE
always-rewritten SiYuan doc — `🏠 Home` in Life — so the operator has a single
place that answers "what needs me right now?":

  🔥 Today        — meetings, talks/events/key-dates on the near horizon
  🎯 Open loops   — open tickets/incidents + what's aging
  ⏰ Reminders    — what's coming due
  ⛪ Ministry     — month hours vs goal + pace
  👥 People       — who you've gone quiet on (shepherd)
  🌱 Spiritual    — study rhythm this week
  🔗 Jump         — links to the fuller surfaces

Each section is built defensively — a single failing source degrades to a
"(unavailable)" line, never a blank page. Loud exit only if SiYuan itself is
unreachable or the doc can't be written. No push: Home is a place you open,
not a ping (the radar digest already covers notable pushes).

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.cockpit_home
"""

import calendar
import os
import sys
from datetime import datetime, timedelta

from loguru import logger

LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
HOME_PATH = "/🏠 Home"
HOME_ATTR = "cockpit-home"

TALK_HORIZON_DAYS = 14
EVENT_HORIZON_DAYS = 7
KEY_DATE_HORIZON_DAYS = 10
REMINDER_HORIZON_DAYS = 7


def _die(msg: str) -> None:
    logger.error(f"Cockpit Home ABORTED: {msg}")
    sys.exit(1)


def _section(title: str, fn) -> list[str]:
    """Build one section defensively: a failing source degrades to a note."""
    try:
        body = fn() or ["_(nothing right now)_"]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Cockpit Home: section '{title}' failed: {e}")
        body = ["_(unavailable)_"]
    return [f"## {title}", *body, ""]


def _age_days(rec: dict, now: datetime) -> int:
    try:
        return (now - datetime.fromisoformat(rec["ts"])).days
    except (KeyError, ValueError):
        return 0


# ── section builders ─────────────────────────────────────────────────────
def _today_lines(now: datetime) -> list[str]:
    from app.jobs.cockpit_radar import (
        _countdown, _key_dates, _prep_state, _upcoming_events, _upcoming_talks,
    )
    from app.graph.tools.spiritual import meetings_today
    from app.graph.tools.capture import local_today
    today = local_today(now)          # operator-local calendar day, not the box's
    out = []

    for label in meetings_today(now):
        out.append(f"- 🎪 **{label} today** — parts, comments, a share ready?")

    speaker = os.getenv("TALK_SYNC_SPEAKER", "").strip().lower()
    for t in _upcoming_talks():
        who = t.get("speaker") or "?"
        try:
            delivery = datetime.strptime(t.get("week_of", ""), "%Y/%m/%d") + timedelta(days=6)
        except ValueError:
            continue
        days = (delivery - today).days
        if 0 <= days <= TALK_HORIZON_DAYS:
            mine = " ← **you**" if speaker and speaker in who.lower() else ""
            out.append(f"- 🎤 {delivery:%a %b %d} · No {t.get('talk_number') or '—'} · "
                       f"{who}{mine} — {_countdown(days)}")

    kd, _ = _key_dates(now)
    for dt, label in kd:
        days = (dt - today).days
        if 0 <= days <= KEY_DATE_HORIZON_DAYS:
            out.append(f"- ⭐ {dt:%a %b %d} · {label} — {_countdown(days)}")

    for ev in _upcoming_events()[:6]:
        start = str(ev.get("start") or ev.get("date") or "")[:10]
        try:
            days = (datetime.fromisoformat(start) - today).days
        except ValueError:
            continue
        if 0 <= days <= EVENT_HORIZON_DAYS:
            name = ev.get("name") or ev.get("title", "?")
            out.append(f"- 📅 {start} · {name} — {_countdown(days)}")

    for p in _prep_state()[:3]:
        out.append(f"- 🗣️ {p}")
    return out


def _loops_lines(now: datetime) -> list[str]:
    from app.graph.tools.capture import open_items
    from app.graph.tools.settings import get_setting
    stale = get_setting("ticket_stale_days")
    opens = open_items()
    if not opens:
        return ["_(no open tickets — inbox zero 🎉)_"]
    inc = sum(1 for r in opens if r.get("kind") == "incident")
    tix = len(opens) - inc
    out = [f"- {tix} open ticket(s), {inc} incident(s)."]
    aging = sorted((r for r in opens if _age_days(r, now) >= stale),
                   key=lambda r: _age_days(r, now), reverse=True)
    for r in aging[:5]:
        tag = "🔴" if r.get("kind") == "incident" else "🎫"
        out.append(f"- ⏳ {tag} `[{r.get('id', '?')}]` {r.get('text', '')} · "
                   f"**{_age_days(r, now)}d** — `close: {r.get('id', '?')}`?")
    return out


def _reminders_lines(now: datetime) -> list[str]:
    from app.graph.tools.capture import _tz, fired_reminder_ids, read_captures
    fired = fired_reminder_ids()
    horizon = now + timedelta(days=REMINDER_HORIZON_DAYS)
    rows = []
    for r in read_captures(kinds={"reminder"}):
        if r.get("id") in fired or not r.get("due"):
            continue
        try:
            when = datetime.fromisoformat(r["due"])
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=_tz())
        n = now if now.tzinfo else now.replace(tzinfo=when.tzinfo)
        h = horizon if horizon.tzinfo else horizon.replace(tzinfo=when.tzinfo)
        if n <= when < h:
            rows.append((when, r.get("text", "")))
    rows.sort(key=lambda x: x[0])
    return [f"- ⏰ {w:%a %b %d %H:%M} — {t}" for w, t in rows[:6]]


def _ministry_lines(now: datetime) -> list[str]:
    from app.graph.tools.capture import month_ministry_hours
    from app.graph.tools.settings import get_setting
    mo = month_ministry_hours(now.year, now.month)
    goal = get_setting("ministry_goal_hours")
    if not goal:
        return [f"- ⛪ {mo}h this month."]
    remaining = max(0.0, goal - mo)
    if remaining <= 0:
        return [f"- ⛪ {mo}h / {goal:.0f}h — **goal met** 🎉"]
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day
    per_day = remaining / days_left if days_left > 0 else remaining
    return [f"- ⛪ {mo}h / {goal:.0f}h — {remaining:.0f}h to go, "
            f"{days_left}d left (~{per_day:.1f}h/day)"]


def _activity_lines(now: datetime) -> list[str]:
    """Adoption signal: Home can only reflect what you feed it. Idle weeks are
    surfaced plainly (with how to start); active weeks show a one-line tally."""
    from app.graph.tools.capture import capture_activity
    a = capture_activity(now, days=7)
    if a["total"] == 0:
        return [
            "- 💤 **0 captures** this week — Home only reflects what you jot.",
            "- Start: `ticket: …` · `note: …` · `person: Jean | …` · "
            "`ministry: 2h` · `remind: tomorrow 9am …`",
        ]
    parts = " · ".join(f"{n} {k}" for k, n in
                       sorted(a["by_kind"].items(), key=lambda x: -x[1]))
    return [f"- ✅ **{a['total']}** capture(s) in the last 7 days — {parts}."]


def _people_lines(now: datetime) -> list[str]:
    from app.graph.tools.people import gone_quiet, group_coverage_lines
    out = []
    for p in gone_quiet(now)[:5]:
        tag = f" · _{', '.join(p['tags'])}_" if p["tags"] else ""
        out.append(f"- 👤 **{p['name']}** · {p['days_since']}d{tag}")
    cov = group_coverage_lines(now, limit=5)
    if cov:
        if out:
            out.append("")
        out.extend(cov)
    return out or ["_(current with your people 🙂)_"]


def _spiritual_lines(now: datetime) -> list[str]:
    from app.graph.tools.spiritual import study_cadence
    from app.graph.tools.settings import get_setting
    n = study_cadence(now, 7)
    goal = int(get_setting("study_goal_days") or 0)
    line = f"- 🌱 Personal study: {n} of the last 7 days"
    line += f" (aiming for {goal})." if goal else "."
    return [line]


def _links_lines(now: datetime) -> list[str]:
    from app.hooks.siyuan_tools import _siyuan_request, get_notebook_id
    nb = get_notebook_id(LIFE_NOTEBOOK)
    out = []
    if nb:
        for path, label in (("/📡 Radar", "📡 Radar"),
                            ("/🧭 Week Ahead", "🧭 Week Ahead"),
                            ("/📥 Capture", "📥 Capture")):
            ids = _siyuan_request("filetree/getIDsByHPath",
                                  {"notebook": nb, "path": path}, quiet=True).get("data") or []
            if ids:
                out.append(f"- (({ids[0]} \"{label}\"))")
    return out or ["_(surfaces build as the pipeline runs)_"]


def _render(now: datetime) -> str:
    from app.graph.tools.capture import to_local
    lines = [f"> 🏠 **Cockpit Home** — refreshed {to_local(now):%Y-%m-%d %H:%M}", ""]
    lines += _section("🗓️ This week", lambda: _activity_lines(now))
    lines += _section("🔥 Today", lambda: _today_lines(now))
    lines += _section("🎯 Open loops", lambda: _loops_lines(now))
    lines += _section("⏰ Reminders", lambda: _reminders_lines(now))
    lines += _section("⛪ Ministry", lambda: _ministry_lines(now))
    lines += _section("👥 People", lambda: _people_lines(now))
    lines += _section("🌱 Spiritual", lambda: _spiritual_lines(now))
    lines += _section("🔗 Jump to", lambda: _links_lines(now))
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    from app.hooks.siyuan_tools import (
        get_notebook_id, siyuan_set_attrs, siyuan_upsert_page,
    )

    now = datetime.now()
    if not get_notebook_id(LIFE_NOTEBOOK):
        _die(f"SiYuan notebook '{LIFE_NOTEBOOK}' unreachable")

    block_id = siyuan_upsert_page(LIFE_NOTEBOOK, HOME_PATH, _render(now))
    if not block_id:
        _die(f"could not write Home doc to {LIFE_NOTEBOOK}{HOME_PATH}")
    siyuan_set_attrs(block_id, {
        "custom-hermes": HOME_ATTR,
        "custom-type": "auto",
        "custom-updated": now.isoformat(timespec="seconds"),
    })
    logger.info(f"Cockpit Home OK: {LIFE_NOTEBOOK}{HOME_PATH} → {block_id}")


if __name__ == "__main__":
    main()
