"""
✍️ Capture — one-line jots routed to the right sink.

The operator texts a verb-prefixed line over WhatsApp and it lands where it
belongs. `data/captures.jsonl` is the single STRUCTURED source of truth that
the evening-wrap / weekly-sitrep / month-end jobs aggregate; the SiYuan
mirrors are best-effort human surfaces — a mirror failure NEVER fails the
capture (the jot is already durably on disk).

Verbs (English by default; Kreyòl aliases in parens also work):
  ticket:   (tiket:)       → work ticket       (mirror: 💼 Work → 🎫 Work Tickets)
  incident: (ensidan:)     → work incident     (mirror: 💼 Work → 🎫 Work Tickets)
  note:     (nòt:/kenbe:)  → quick note        (mirror: Life → 📥 Capture)
  person:   (moun:)        → person touch      (mirror: 📥 Capture; feeds the shepherd)
  ministry: (sèvis:)       → pioneer hours     (tally derived from jsonl; "2h", "90min")
  business: (biznis:)      → business inbox    (mirror: 💼 Work → 🛒 Business Inbox)
  close:    (fèmen:)       → close a ticket    (by its short id, e.g. `close: a3f2`)
  remind:   (sonje:)       → timed reminder    (fires at the parsed time, e.g.
                                                `remind: tomorrow 2pm call the brother`)
  family:   (nou:/us:)     → family / marriage (mirror: Life → ❤️ Family)
  study:    (etid:)        → spiritual study   (mirror: Life → 📖 Study)

Tickets/incidents get a short id at capture time (shown in the confirmation)
so `close: <id>` can retire them; the jobs list only still-open items.
Reminders are parsed into a due time (Haiti-local) and fired by the
`reminders` job; family/study notes serve the operator's stated top
priorities (spouse, spiritual) that the work-heavy cockpit under-served.

Operator-only (toolset "capture"): it writes to the operator's own notes and
logs. Never grant it to a tier.
"""

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta

from loguru import logger

CAPTURES_PATH = os.getenv(
    "CAPTURES_PATH", "/opt/iiab/ai-gateway/data/captures.jsonl"
)
LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
TRAVAY_NOTEBOOK = os.getenv("TRAVAY_NOTEBOOK", "💼 Work")
# Reminders are parsed + fired in the operator's local tz, not the box's
# system clock (this box runs CEST; the operator is in Haiti). Everything on
# the reminder path is tz-aware; the rest of capture stays naive.
TZ = os.getenv("COCKPIT_TZ", "America/Port-au-Prince")

# mirror doc paths (best-effort)
KAPTU_PATH = "/📥 Capture"
TICKETS_PATH = "/🎫 Work Tickets"
BUSINESS_PATH = "/🛒 Business Inbox"
FAMILY_PATH = "/❤️ Family"
STUDY_PATH = "/📖 Study"

# verb → canonical kind. English is the documented default; Kreyòl and
# common-misspelling aliases are accepted too (routing is verb-agnostic).
VERBS = {
    "tiket": "ticket", "ticket": "ticket", "tikè": "ticket",
    "ensidan": "incident", "insidan": "incident", "incident": "incident",
    "kenbe": "note", "nòt": "note", "not": "note", "note": "note", "jot": "note",
    "moun": "person", "person": "person",
    "sèvis": "ministry", "sevis": "ministry", "service": "ministry",
    "predikasyon": "ministry", "ministry": "ministry",
    "biznis": "business", "business": "business",
    "fèmen": "close", "femen": "close", "close": "close",
    "resoud": "close", "resolve": "close",
    "sonje": "reminder", "remind": "reminder", "reminder": "reminder",
    "nou": "family", "us": "family", "fanmi": "family", "family": "family",
    "etid": "study", "etidye": "study", "study": "study", "spiritual": "study",
    # note lifecycle: promote a note into a ticket, or archive it
    "promote": "promote", "pwomote": "promote", "pwomouvwa": "promote",
    "done": "note_done", "fini": "note_done", "archive": "note_done",
    "achiv": "note_done", "klase": "note_done",
}

# canonical kind → English label for confirmations
KIND_LABEL = {
    "ticket": "Ticket", "incident": "Incident", "note": "Note",
    "person": "Person", "ministry": "Ministry", "business": "Business",
    "reminder": "Reminder", "family": "Family", "study": "Study",
    "promote": "Promoted", "note_done": "Archived",
}


def _now() -> datetime:
    return datetime.now()


def to_local(now: datetime | None = None) -> datetime:
    """`now` (naive box-clock or aware) as an AWARE datetime in the operator's
    tz — for honest display of when a box-run job actually happened for them."""
    box = now or datetime.now()
    return box.astimezone().astimezone(_tz())


def local_day_start(now: datetime | None = None) -> datetime:
    """Box-naive threshold for the START of the operator's *local* calendar day
    containing `now`. Captures are stamped naive on the box clock (this box runs
    CEST) but the operator lives hours behind (Haiti). A job that reckons 'today'
    straight off the box clock and fires after box-midnight — the evening wrap
    runs 20:30 Haiti = 02:30 box — reads a box-day that has already rolled over,
    so `read_captures(since=box_midnight)` misses the whole operator day. Anchor
    'today' to the operator's OWN midnight, expressed back in the box's naive
    frame so it lines up with the naive capture timestamps."""
    local = to_local(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone().replace(tzinfo=None)  # → box-naive


def local_today(now: datetime | None = None) -> datetime:
    """The operator's LOCAL calendar day as a naive midnight datetime — for
    calendar 'days until' countdowns against naive date-midnight targets (talk
    and event dates). Distinct from local_day_start(), which returns a box-naive
    INSTANT for filtering box-stamped capture timestamps: this one stays on the
    operator's wall clock so `(target - local_today(now)).days` counts calendar
    days the way the operator would. The box runs CEST, hours ahead of Haiti, so
    a box-midnight reference is a full day off whenever the box date has rolled
    past the operator's (e.g. any run between box 00:00 and 06:00)."""
    return to_local(now).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def _short_id(ts: str, text: str) -> str:
    """A stable 4-char id for a ticket/incident, so it can be closed later."""
    return hashlib.sha1(f"{ts}|{text}".encode("utf-8")).hexdigest()[:4]


def parse_hours(text: str) -> float | None:
    """Pull a duration out of a ministry line: '2h', '1.5h', '90m', '90 min',
    '2 èdtan'. Returns hours as float, or None if no duration found."""
    t = text.lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(h|hr|hrs|è|èdtan|edtan|hour|hours)\b", t)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"(\d+)\s*(m|min|mins|minit|minutes?)\b", t)
    if m:
        return round(int(m.group(1)) / 60.0, 2)
    return None


def split_verb(line: str) -> tuple[str | None, str]:
    """Split a raw 'verb: rest' line. Returns (canonical_kind|None, rest)."""
    line = (line or "").strip()
    m = re.match(r"^\s*([A-Za-zÀ-ÿ]+)\s*[:：]\s*(.*)$", line, re.DOTALL)
    if not m:
        return None, line
    verb = m.group(1).strip().lower()
    return VERBS.get(verb), m.group(2).strip()


def _operator_digits() -> set[str]:
    return {re.sub(r"\D", "", p) for p in os.getenv("ADMIN_PHONE", "").split(",") if p.strip()}


def is_operator_capture(user_id: str, text: str) -> bool:
    """True when the caller is the operator and the line starts with a capture verb.

    The chat path files such a line straight into its sink, before any model turn:
    the verb decided what the line is, so no model gets to decide whether to keep it.
    The capture toolset is never grantable, so only the operator's identities count.
    """
    digits = re.sub(r"\D", "", (user_id or "").split(":")[-1])
    if not digits or digits not in _operator_digits():
        return False
    kind, _rest = split_verb(text)
    return kind is not None


def _append_jsonl(record: dict) -> None:
    """Atomic-enough append to the structured source of truth."""
    os.makedirs(os.path.dirname(CAPTURES_PATH), exist_ok=True)
    with open(CAPTURES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_captures(
    since: datetime | None = None,
    until: datetime | None = None,
    kinds: set[str] | None = None,
) -> list[dict]:
    """Shared reader for the jobs. Returns records in [since, until) that match
    kinds. Never raises — a missing/garbled file yields []."""
    out: list[dict] = []
    try:
        with open(CAPTURES_PATH, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if kinds and rec.get("kind") not in kinds:
                    continue
                ts = rec.get("ts")
                if (since or until) and ts:
                    try:
                        when = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                    if since and when < since:
                        continue
                    if until and when >= until:
                        continue
                out.append(rec)
    except OSError:
        return []
    return out


def closed_ids() -> set[str]:
    """The set of ticket/incident ids retired by a `close:` capture (all time)."""
    ids: set[str] = set()
    for rec in read_captures(kinds={"close"}):
        ids.update(rec.get("refs") or [])
    return ids


def open_items(
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Ticket/incident records in the window that have NOT been closed."""
    done = closed_ids()
    return [
        r for r in read_captures(since=since, until=until, kinds={"ticket", "incident"})
        if r.get("id") not in done
    ]


def _find_capture_by_id(cid: str, kind: str | None = None) -> dict | None:
    """Newest capture with this short id (optionally of one kind), or None."""
    cid = (cid or "").lower()
    if not cid:
        return None
    hit = None
    for r in read_captures(kinds={kind} if kind else None):
        if r.get("id") == cid:
            hit = r  # keep scanning so the newest wins
    return hit


def archived_note_ids() -> set[str]:
    """Note ids retired by a `done:`/`archive:` or `promote:` capture (all time)."""
    ids: set[str] = set()
    for rec in read_captures(kinds={"note_done"}):
        ids.update(rec.get("refs") or [])
    return ids


def open_notes(
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Note records in the window that carry an id and have NOT been
    archived/promoted. Id-less legacy notes have no lifecycle and are omitted."""
    done = archived_note_ids()
    return [
        r for r in read_captures(since=since, until=until, kinds={"note"})
        if r.get("id") and r.get("id") not in done
    ]


# machine-generated / bookkeeping kinds — never counted as operator activity
_NOISE_KINDS = {"reminder_fired", "close", "note_done"}


def capture_activity(now: datetime | None = None, days: int = 7) -> dict:
    """Adoption signal: how much the operator has actually captured. Returns
    {window_days, total, by_kind, last_ts, days_since_last}. Bookkeeping kinds
    (fired markers, closes, archives) don't count as activity."""
    now = now or _now()
    since = (now - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    last_ts = None
    for r in read_captures():
        if r.get("kind") in _NOISE_KINDS:
            continue
        ts = r.get("ts")
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
    by_kind: dict[str, int] = {}
    for r in read_captures(since=since):
        k = r.get("kind")
        if k in _NOISE_KINDS:
            continue
        by_kind[k] = by_kind.get(k, 0) + 1
    days_since = None
    if last_ts:
        try:
            days_since = (now - datetime.fromisoformat(last_ts)).days
        except ValueError:
            pass
    return {
        "window_days": days,
        "total": sum(by_kind.values()),
        "by_kind": by_kind,
        "last_ts": last_ts,
        "days_since_last": days_since,
    }


def month_ministry_hours(year: int, month: int) -> float:
    """Total pioneer hours captured in a calendar month, reckoned in the
    operator's local tz. A Haiti-evening entry is stamped on the box's NEXT
    day (box runs CEST, hours ahead), so a naive `ts[:7]` would misfile hours
    across a month boundary — count by the operator's own local month instead.
    Callers must pass the operator-local year/month (see to_local)."""
    total = 0.0
    for rec in read_captures(kinds={"ministry"}):
        try:
            lt = to_local(datetime.fromisoformat(rec.get("ts", "")))
        except ValueError:
            continue
        if lt.year == year and lt.month == month:
            total += float(rec.get("hours") or 0)
    return round(total, 2)


# ── Reminders: tz-aware time parsing + fire tracking ─────────────────────
def _tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(TZ)


def _local_now() -> datetime:
    """Now in the operator's tz (aware) — the frame reminders live in."""
    return datetime.now(_tz())


_UNIT_MIN = {"m", "min", "mins", "minute", "minutes", "minit"}
_UNIT_HR = {"h", "hr", "hrs", "hour", "hours", "è", "èdtan", "edtan"}
_UNIT_DAY = {"d", "day", "days", "jou"}
_UNIT_WK = {"wk", "week", "weeks", "semèn", "semen"}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "lendi": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "madi": 1,
    "wednesday": 2, "wed": 2, "mèkredi": 2, "mekredi": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "jedi": 3,
    "friday": 4, "fri": 4, "vandredi": 4,
    "saturday": 5, "sat": 5, "samdi": 5,
    "sunday": 6, "sun": 6, "dimanch": 6,
}

_TIME_TOKEN_RE = re.compile(
    r"(?<![\d/])(?:at\s+|nan\s+|a\s+|à\s+)?"
    r"(\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm)|\d{1,2}\s*[hè]\s*\d{2}|\d{1,2}\s*[hè](?![a-zè])"
    r"|noon|midnight|midi|minwi)(?![\d:])",
    re.I,
)

# Absolute calendar dates — the case the July 2026 funeral reminder got wrong:
# "30 juil. 2026 08:00", "15 septanm 2026", "Sept 3", "12/09". Month names in
# English, French and Kreyòl (with the usual abbreviations); day/month numeric
# order is dd/mm (Haiti). Year optional → the next occurrence.
_MONTHS = {
    1: ("january", "jan", "janvier", "janv", "janvye"),
    2: ("february", "feb", "février", "fevrier", "févr", "fevr", "fév", "fev", "fevriye", "fevriyè"),
    3: ("march", "mar", "mars", "mas"),
    4: ("april", "apr", "avril", "avr"),
    5: ("may", "mai", "me"),
    6: ("june", "jun", "juin", "jen"),
    7: ("july", "jul", "juillet", "juil", "jiyè", "jiye"),
    8: ("august", "aug", "août", "aout", "out", "dawou"),
    9: ("september", "sep", "sept", "septembre", "septanm"),
    10: ("october", "oct", "octobre", "oktòb", "oktob"),
    11: ("november", "nov", "novembre", "novanm"),
    12: ("december", "dec", "déc", "décembre", "decembre", "desanm"),
}
_MONTH_LOOKUP = {name: n for n, names in _MONTHS.items() for name in names}
_MONTH_ALT = "|".join(sorted((re.escape(n) for n in _MONTH_LOOKUP), key=len, reverse=True))
_DATE_DMY_RE = re.compile(          # 30 juil. 2026 · 15 septanm · 1er janvier
    r"\b(\d{1,2})(?:er|e|st|nd|rd|th)?\s+(" + _MONTH_ALT + r")\.?(?:\s+|,\s*)?(\d{4})?\b",
    re.I,
)
_DATE_MDY_RE = re.compile(          # Sept 3, 2026 · July 30
    r"\b(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b",
    re.I,
)
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")   # dd/mm[/yyyy]


def _absolute_date(text: str, now: datetime):
    """(date, span) for an explicit calendar date in *text*, else (None, None).
    A missing year means the next occurrence on/after today."""
    low = text.lower()
    cands = []
    for m in _DATE_DMY_RE.finditer(low):
        mon = _MONTH_LOOKUP.get(m.group(2).lower())
        if mon:
            cands.append((int(m.group(1)), mon, m.group(3), m.span()))
    for m in _DATE_MDY_RE.finditer(low):
        mon = _MONTH_LOOKUP.get(m.group(1).lower())
        if mon:
            cands.append((int(m.group(2)), mon, m.group(3), m.span()))
    if not cands:
        for m in _DATE_NUM_RE.finditer(low):
            d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            if y and len(y) == 2:
                y = "20" + y
            cands.append((d, mo, y, m.span()))
    for day, mon, year, span in cands:
        try:
            if year:
                return date(int(year), mon, day), span
            cand = date(now.year, mon, day)
            if cand < now.date():
                cand = date(now.year + 1, mon, day)
            return cand, span
        except ValueError:
            continue
    return None, None
_REL_RE = re.compile(
    r"\b(?:in|nan|apre)\s+(\d+)\s*"
    r"(minutes?|mins?|minit|hours?|hrs?|hour|è|èdtan|edtan|days?|jou"
    r"|weeks?|semèn|semen|wk|m|h|d)\b",
    re.I,
)


def _parse_tod(s: str) -> tuple[int, int] | None:
    """A time-of-day token → (hour, minute), or None."""
    s = s.strip().lower()
    if s in ("noon", "midi"):
        return 12, 0
    if s in ("midnight", "minwi"):
        return 0, 0
    s = re.sub(r"\s+", "", s)
    m = re.fullmatch(r"(\d{1,2})[hè](\d{2})?", s)  # 14h30 / 14h / 7è / 8è30 (fr/kreyòl)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h < 24 and mi < 60:
            return h, mi
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if h < 24 and mi < 60:
            return h, mi
    return None


def parse_when(text: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Pull a due time out of a reminder line. Returns (aware due | None,
    remaining message). Understands: 'in 30 min', 'nan 2 èdtan', 'tomorrow
    2pm', 'demen', 'tonight', weekday names (en/kreyòl), 'at 14:00', '9am',
    '14h30', '7è', ISO '2026-07-15 14:00', and explicit calendar dates in
    en/fr/kreyòl ('30 juil. 2026 08:00', '15 septanm', 'Sept 3', '12/09').
    An explicit date wins over 'tomorrow'/weekday words. A lone bare number
    is NOT a time (too ambiguous). No time found → (None, text)."""
    if now is None:
        now = _local_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz())
    low = text.lower()
    spans: list[tuple[int, int]] = []
    due: datetime | None = None

    m = _REL_RE.search(low)
    if m:
        n, u = int(m.group(1)), m.group(2).lower()
        if u in _UNIT_MIN:
            due = now + timedelta(minutes=n)
        elif u in _UNIT_HR:
            due = now + timedelta(hours=n)
        elif u in _UNIT_DAY:
            due = now + timedelta(days=n)
        elif u in _UNIT_WK:
            due = now + timedelta(weeks=n)
        if due:
            spans.append(m.span())

    if due is None:
        tod = None
        _abs_day, _abs_span = _absolute_date(text, now)
        masked = text
        if _abs_span:
            masked = text[:_abs_span[0]] + " " * (_abs_span[1] - _abs_span[0]) + text[_abs_span[1]:]
        mt = _TIME_TOKEN_RE.search(masked)
        if mt:
            parsed = _parse_tod(mt.group(1))
            if parsed:
                tod = parsed
                spans.append(mt.span())

        base = None            # a specific calendar day
        roll_week = False      # weekday-in-the-past → next week
        abs_day, abs_span = _absolute_date(text, now)
        if abs_day is not None:
            base = abs_day
            spans.append(abs_span)
        elif (m := re.search(r"\b(tomorrow|demen)\b", low)):
            base = (now + timedelta(days=1)).date()
            spans.append(m.span())
        elif (m := re.search(r"\b(tonight|aswè|aswe)\b", low)):
            base = now.date()
            spans.append(m.span())
            if tod is None:
                tod = (19, 0)
        elif (m := re.search(r"\b(today|jodi\s*a|jodia)\b", low)):
            base = now.date()
            spans.append(m.span())
        elif (m := re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", low)):
            target = _WEEKDAYS[m.group(1)]
            delta = (target - now.weekday()) % 7
            spans.append(m.span())
            base = (now + timedelta(days=delta)).date()
            roll_week = delta == 0  # same weekday: may need +7 if time passed
        elif (m := re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)):
            try:
                base = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                spans.append(m.span())
            except ValueError:
                base = None

        if base is not None or tod is not None:
            h, mi = tod if tod else (9, 0)
            day = base if base is not None else now.date()
            cand = datetime(day.year, day.month, day.day, h, mi, tzinfo=_tz())
            if cand <= now:
                if base is None:
                    cand += timedelta(days=1)   # bare time → next occurrence
                elif roll_week:
                    cand += timedelta(days=7)    # this weekday already passed
            due = cand

    remainder = text
    for s, e in sorted(spans, reverse=True):
        remainder = remainder[:s] + remainder[e:]
    remainder = re.sub(r"\s+", " ", remainder).strip(" ,.:-—–")
    remainder = re.sub(
        r"^(?:to|about|that|me to|remember to|pou|ke|pou m|pou mwen)\s+",
        "", remainder, flags=re.I,
    ).strip()
    return due, remainder


def _fmt_due(due: datetime, now: datetime | None = None) -> str:
    """Human due label in the operator's tz."""
    now = now or _local_now()
    d0, dd = now.date(), due.date()
    t = due.strftime("%H:%M")
    if dd == d0:
        return f"today {t}"
    if dd == d0 + timedelta(days=1):
        return f"tomorrow {t}"
    if 0 < (dd - d0).days < 7:
        return due.strftime(f"%a {t}")
    return due.strftime(f"%a %b %d, {t}")


def fired_reminder_ids() -> set[str]:
    """Reminder ids already delivered (kind='reminder_fired', all time)."""
    ids: set[str] = set()
    for rec in read_captures(kinds={"reminder_fired"}):
        ids.update(rec.get("refs") or [])
    return ids


def due_reminders(now: datetime | None = None) -> list[dict]:
    """Reminders whose due time has arrived and that have not fired yet."""
    if now is None:
        now = _local_now()
    fired = fired_reminder_ids()
    out: list[dict] = []
    for r in read_captures(kinds={"reminder"}):
        if r.get("id") in fired or not r.get("due"):
            continue
        try:
            when = datetime.fromisoformat(r["due"])
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=_tz())
        if when <= now:
            out.append(r)
    return out


def mark_reminders_fired(ids: list[str]) -> None:
    """Append the fired-state marker so a reminder never fires twice."""
    ids = [i for i in ids if i]
    if not ids:
        return
    now = _now()
    _append_jsonl({
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "kind": "reminder_fired",
        "refs": ids,
    })


def _mirror(kind: str, text: str, rec: dict) -> None:
    """Best-effort SiYuan surface. Any failure is swallowed — jsonl is truth."""
    try:
        from app.hooks.siyuan_tools import siyuan_append_block
    except Exception as e:  # noqa: BLE001
        logger.warning(f"capture: SiYuan mirror unavailable: {e}")
        return
    stamp = _now().strftime("%Y-%m-%d %H:%M")
    try:
        if kind in ("ticket", "incident"):
            tag = "🔴 INCIDENT" if kind == "incident" else "🎫"
            siyuan_append_block(
                TRAVAY_NOTEBOOK, TICKETS_PATH,
                f"- {tag} `[{rec.get('id', '?')}]` `{stamp}` — {text}  #open",
            )
        elif kind == "close":
            siyuan_append_block(
                TRAVAY_NOTEBOOK, TICKETS_PATH,
                f"- ✅ `{stamp}` closed: {', '.join(rec.get('refs') or [])}",
            )
        elif kind == "business":
            siyuan_append_block(
                TRAVAY_NOTEBOOK, BUSINESS_PATH, f"- `{stamp}` — {text}",
            )
        elif kind == "ministry":
            h = rec.get("hours")
            suffix = f" — {h}h" if h else ""
            siyuan_append_block(
                LIFE_NOTEBOOK, KAPTU_PATH,
                f"- ⛪ `{stamp}` ministry{suffix}: {text}",
            )
        elif kind == "family":
            siyuan_append_block(
                LIFE_NOTEBOOK, FAMILY_PATH, f"- ❤️ `{stamp}` — {text}",
            )
        elif kind == "study":
            siyuan_append_block(
                LIFE_NOTEBOOK, STUDY_PATH, f"- 📖 `{stamp}` — {text}",
            )
        elif kind == "reminder":
            due = rec.get("due", "")
            siyuan_append_block(
                LIFE_NOTEBOOK, KAPTU_PATH,
                f"- ⏰ `{stamp}` reminder → `{due}`: {text}",
            )
        elif kind == "note_done":
            siyuan_append_block(
                LIFE_NOTEBOOK, KAPTU_PATH,
                f"- 🗄️ `{stamp}` archived: {', '.join(rec.get('refs') or [])}",
            )
        else:  # note, person
            icon = "👤" if kind == "person" else "📝"
            siyuan_append_block(
                LIFE_NOTEBOOK, KAPTU_PATH, f"- {icon} `{stamp}` — {text}",
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"capture: mirror write failed ({kind}): {e}")


def macro_capture(args: dict, **kwargs) -> str:
    """Route a one-line jot to its sink. Serves both callers:
      - Hermes  → {"kind": "ticket", "text": "printer HR pa mache"}
      - bridge  → {"_args": ["ticket", "printer", "HR", ...]}  (verb-prefixed)
    or a raw {"text": "ticket: HR printer offline"} line."""
    text = (args.get("text") or "").strip()
    kind = (args.get("kind") or "").strip().lower() or None

    # bridge style: _args tokens, first may be the kind
    raw = args.get("_args")
    if raw and not text:
        if raw and raw[0].lower() in VERBS and not kind:
            kind, text = raw[0].lower(), " ".join(raw[1:]).strip()
        else:
            text = " ".join(raw).strip()

    if kind in VERBS:
        kind = VERBS[kind]
    if not kind:
        kind, text = split_verb(text)
    if not kind:
        return (
            "🤔 What kind? Use a verb: `ticket:` `incident:` `note:` `person:` "
            "`ministry:` `business:` `remind:` `family:` `study:` `close:`"
        )
    # SiYuan-origin captures pass mirror=False — they already live in SiYuan, so
    # re-mirroring would double-surface them. WhatsApp captures mirror (default).
    do_mirror = args.get("mirror") is not False
    if not text:
        if kind == "close":
            return "🤔 Close what? Give the ticket id, e.g. `close: a3f2`."
        if kind == "note_done":
            return "🤔 Archive which note? Give its id, e.g. `done: a3f2`."
        if kind == "promote":
            return "🤔 Promote which note? Give its id, e.g. `promote: a3f2`."
        return f"🤔 {KIND_LABEL.get(kind, kind)} is empty — write something to capture."

    now = _now()
    ts = now.isoformat(timespec="seconds")
    rec = {
        "ts": ts,
        "date": now.strftime("%Y-%m-%d"),
        "kind": kind,
        "text": text,
    }

    # Close: retire ticket/incident ids referenced in the text. Verify each id
    # against real ticket/incident captures — an id that matches nothing must
    # NOT report "closed" (it silently poisoned the sitrep before).
    if kind == "close":
        refs = re.findall(r"[0-9a-f]{4}", text.lower())
        if not refs:
            return "🤔 Close what? Give the ticket id, e.g. `close: a3f2`."
        known = {r["id"] for r in read_captures(kinds={"ticket", "incident"})
                 if r.get("id")}
        already = closed_ids()
        matched = [x for x in refs if x in known]
        unknown = [x for x in refs if x not in known]
        # only the ids that aren't already closed actually retire something
        newly = [x for x in matched if x not in already]
        if not newly:
            bits = []
            if matched:
                bits.append(f"already closed: {', '.join(matched)}")
            if unknown:
                bits.append(f"no such id: {', '.join(unknown)}")
            return "🤔 Nothing to close — " + " · ".join(bits) + "."
        rec["refs"] = newly
        _append_jsonl(rec)
        if do_mirror:
            _mirror(kind, text, rec)
        msg = f"✅ Closed: {', '.join(newly)}"
        extra = []
        if unknown:
            extra.append(f"⚠️ no such id: {', '.join(unknown)}")
        already_hit = [x for x in matched if x in already]
        if already_hit:
            extra.append(f"already closed: {', '.join(already_hit)}")
        if extra:
            msg += " · " + " · ".join(extra)
        return msg

    # Archive a note (done:/archive:) — retire it from the open-notes view.
    if kind == "note_done":
        refs = re.findall(r"[0-9a-f]{4}", text.lower())
        rec["refs"] = refs
        _append_jsonl(rec)
        if do_mirror:
            _mirror(kind, text, rec)
        if refs:
            return f"🗄️ Archived note(s): {', '.join(refs)}"
        return "🤔 Archive which note? Give its id, e.g. `done: a3f2`."

    # Promote a note into a work ticket, then archive the source note.
    if kind == "promote":
        refs = re.findall(r"[0-9a-f]{4}", text.lower())
        if not refs:
            return "🤔 Promote which note? Give its id, e.g. `promote: a3f2`."
        src = _find_capture_by_id(refs[0], kind="note")
        if not src:
            return f"🤔 No note with id `{refs[0]}` to promote."
        if refs[0] in archived_note_ids():
            return f"🤔 Note `{refs[0]}` was already archived/promoted."
        ttext = src.get("text", "")
        tid = _short_id(ts, ttext)
        trec = {"ts": ts, "date": rec["date"], "kind": "ticket",
                "text": ttext, "id": tid, "status": "open", "from_note": refs[0]}
        _append_jsonl(trec)
        if do_mirror:
            _mirror("ticket", ttext, trec)
        _append_jsonl({"ts": ts, "date": rec["date"], "kind": "note_done",
                       "refs": [refs[0]], "text": f"promoted → {tid}"})
        return (f"🎫 Promoted note `{refs[0]}` → ticket `[{tid}]`: {ttext}  "
                f"(close with `close: {tid}`)")

    # Reminder: parse a due time; the `reminders` job fires it at that time.
    if kind == "reminder":
        due, body = parse_when(text)
        if not due:
            return ("🤔 When? Add a time, e.g. `remind: tomorrow 2pm call the "
                    "brother`, `remind: in 3h ...`, or `remind: Thu 9am ...`.")
        rec["id"] = _short_id(ts, text)
        rec["due"] = due.isoformat(timespec="minutes")
        rec["text"] = body or text
        _append_jsonl(rec)
        if do_mirror:
            _mirror(kind, body or text, rec)
        return f"⏰ Reminder set for **{_fmt_due(due)}**: {body or text}"

    if kind in ("ticket", "incident"):
        rec["id"] = _short_id(ts, text)
        rec["status"] = "open"
    if kind == "note":
        rec["id"] = _short_id(ts, text)
    if kind == "ministry":
        rec["hours"] = parse_hours(text)
    if kind == "person":
        # Attach the touch to a person (name up front) so the shepherd view can
        # track last-contact + "who's gone quiet". Stored on the record so the
        # reader need not re-parse; free-text jots stay valid (person=None).
        from app.graph.tools.people import parse_person
        pname, note, tags = parse_person(text)
        if pname:
            rec["person"] = pname
            rec["note"] = note
        if tags:
            rec["tags"] = tags

    _append_jsonl(rec)
    if do_mirror:
        _mirror(kind, text, rec)

    if kind == "ministry":
        h = rec.get("hours")
        mo = month_ministry_hours(now.year, now.month)
        got = f"{h}h " if h else ""
        return f"✅ Ministry {got}logged — month total: **{mo}h**."
    if kind in ("ticket", "incident"):
        label = "Incident" if kind == "incident" else "Ticket"
        return f"✅ {label} `[{rec['id']}]` logged: {text}  (close with `close: {rec['id']}`)"
    if kind == "business":
        return f"✅ Business inbox: {text}"
    if kind == "family":
        return f"❤️ Family note saved: {text}"
    if kind == "study":
        return f"📖 Study note saved: {text}"
    if kind == "person":
        if rec.get("person"):
            return f"👤 Noted about **{rec['person']}**: {rec.get('note') or text}"
        return f"👤 Person note saved: {text}"
    if kind == "note":
        return (f"📝 Note `[{rec['id']}]` saved: {text}  "
                f"(→ ticket: `promote: {rec['id']}` · archive: `done: {rec['id']}`)")
    return f"✅ Captured ({KIND_LABEL.get(kind, kind)}): {text}"
