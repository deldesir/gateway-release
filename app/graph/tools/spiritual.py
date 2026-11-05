"""
🕊️ Spiritual cadence — gentle, deterministic nudges for the things that come
first, without ever standing between the operator and the source.

Three light habits, all nudge-only (no content is fetched or reproduced):

  - Daily text — a morning nudge to read today's text *in JW Library*. The
    operator reads it there; this only reminds. (`daily_text_nudge` on/off,
    snoozable `daily_text`.)
  - Meeting prep — on a configured midweek/weekend meeting day, a morning
    "meeting today — prepared?" nudge (`midweek_meeting_day` /
    `weekend_meeting_day`, snoozable `meeting_prep`).
  - Study rhythm — a weekly (Monday) reflection on how many of the last 7
    days carried a `study:` capture, encouraging toward `study_goal_days`.

The reader is deterministic and zero-token; the `spiritual_nudge` job pushes
one combined morning ping (WhatsApp — the habit channel, like reminders).
"""

from datetime import datetime, timedelta

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_LABELS = {
    "midweek_meeting_day": "Midweek meeting",
    "weekend_meeting_day": "Weekend meeting",
}


def _setting(key: str, default=None):
    from app.graph.tools.settings import get_setting
    v = get_setting(key)
    return default if v is None else v


def meetings_today(now: datetime | None = None) -> list[str]:
    """Labels of any configured meetings falling on `now`'s weekday."""
    now = now or datetime.now()
    out = []
    for key, label in _LABELS.items():
        day = str(_setting(key, "") or "").strip().lower()
        if day in _WEEKDAYS and _WEEKDAYS[day] == now.weekday():
            out.append(label)
    return out


def study_cadence(now: datetime | None = None, days: int = 7) -> int:
    """Distinct days in the last `days` that carry a `study:` capture."""
    from app.graph.tools.capture import read_captures
    now = now or datetime.now()
    since = (now - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    seen = set()
    for r in read_captures(since=since, kinds={"study"}):
        d = (r.get("date") or (r.get("ts") or "")[:10])
        if d:
            seen.add(d)
    return len(seen)


def daily_text_line() -> str | None:
    """The daily-text nudge, if enabled. Content lives in JW Library — this
    only reminds; it never fetches or reproduces the text."""
    if not _setting("daily_text_nudge", True):
        return None
    return "📖 Today's text — a moment in JW Library before the day starts."


def meeting_line(now: datetime | None = None) -> str | None:
    labels = meetings_today(now)
    if not labels:
        return None
    which = " & ".join(labels)
    return f"🎪 {which} today — parts, comments, and a share ready?"


def study_line(now: datetime | None = None) -> str | None:
    """A weekly (Monday) rhythm reflection — encouragement, never a scold."""
    now = now or datetime.now()
    if now.weekday() != 0:            # Mondays only, to avoid daily nagging
        return None
    n = study_cadence(now, 7)
    goal = int(_setting("study_goal_days", 0) or 0)
    if goal > 0:
        if n >= goal:
            return f"🌱 Personal study: {n}/7 days last week — goal met. 🙂"
        return (f"🌱 Personal study: {n} of the last 7 days (aiming for {goal}). "
                f"A little each day compounds.")
    if n == 0:
        return None                   # nothing logged, no goal → stay silent
    return f"🌱 Personal study: logged on {n} of the last 7 days."


def build_nudge(now: datetime | None = None) -> str | None:
    """The combined morning spiritual nudge, or None if there's nothing to say
    (or the whole thing is snoozed). Each line is independently snoozable."""
    from app.graph.tools.settings import is_snoozed
    now = now or datetime.now()
    if is_snoozed("spiritual", now):
        return None
    lines = []
    if not is_snoozed("daily_text", now) and (dt := daily_text_line()):
        lines.append(dt)
    if not is_snoozed("meeting_prep", now) and (mt := meeting_line(now)):
        lines.append(mt)
    if (st := study_line(now)):
        lines.append(st)
    if not lines:
        return None
    return "🕊️ *Morning* —\n" + "\n".join(lines)
