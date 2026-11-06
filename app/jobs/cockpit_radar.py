"""
📡 Radar — the cockpit's one honest auto-surface.

Every morning this gathers reality from the systems that know it and
rewrites ONE machine-owned SiYuan doc (Life → 📡 Radar) that the 🏠 Home
cockpit links to:

  - upcoming public talks (Organized), the operator's own flagged
  - upcoming congregation events (Organized)
  - personal key dates the operator maintains BY HAND in Life → 📅 Key Dates
    (job start, deadlines, appointments — anything no system knows about)
  - talk-prep state (talkmaster revisions in the active window)
  - systemd units currently in failed state

It also emails a digest via the bridge — but ONLY when something is
notable (a failed unit, an own-talk or key date within 10 days, an
event within 7 days). A digest that always fires trains the operator
to ignore it; the Radar doc itself is always refreshed regardless.
The change-detection hash covers the notable FACTS (unit names, dates,
labels), never rendered countdowns — "in 9 days" becoming "in 8 days"
must not re-fire the email.

Every data source degrades gracefully: a dead backend or missing table
becomes a "(unavailable)" line, never a crash — the Radar must render
even when the systems it watches are the story.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.cockpit_radar
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from loguru import logger

LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
STATE_FILE = os.getenv(
    "RADAR_STATE_FILE", "/opt/iiab/ai-gateway/data/radar_last_digest.hash"
)
RADAR_PATH = "/📡 Radar"
KEY_DATES_PATH = os.getenv("RADAR_KEY_DATES_PATH", "/📅 Key Dates")
OWN_TALK_SOON_DAYS = 10
EVENT_SOON_DAYS = 7
KEY_DATE_SOON_DAYS = int(os.getenv("RADAR_KEY_DATE_SOON_DAYS", "10"))

KEY_DATES_SEED = """> ✍️ YOUR page — write your important dates here, one line per date:
> `- YYYY-MM-DD · label`. 📡 Radar reads this page every morning and sends
> a digest when a date is approaching. Past dates are ignored.

- 2026-01-01 · 🎉 Example (delete this and add your own dates)
"""

_KEY_DATE_RE = re.compile(
    r"^\s*[-*]\s*(\d{4}-\d{2}-\d{2})\s*(?:[·—–:|-]\s*)?(.*\S)?", re.MULTILINE
)


def _upcoming_talks() -> list[dict]:
    try:
        from app.graph.tools.organized import query_organized_api
        data = json.loads(query_organized_api(
            {"action": "get_upcoming_talks", "weeks": 3}))
        return data.get("talks", [])
    except Exception as e:
        logger.warning(f"Radar: talks unavailable: {e}")
        return []


def _upcoming_events() -> list[dict]:
    try:
        from app.graph.tools.organized import query_organized_api
        data = json.loads(query_organized_api({"action": "get_events"}))
        return data.get("events", [])
    except Exception as e:
        logger.warning(f"Radar: events unavailable: {e}")
        return []


def _failed_units() -> list[str]:
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--state=failed", "--plain",
             "--no-legend"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return [ln.split()[0] for ln in out.splitlines() if ln.strip()]
    except Exception as e:
        logger.warning(f"Radar: systemd query failed: {e}")
        return []


def _key_dates(now: datetime) -> tuple[list[tuple[datetime, str]], str | None]:
    """Read the operator's hand-maintained 📅 Key Dates page.

    Self-seeding: if the page doesn't exist yet it is created with format
    instructions and a sample date, so a fresh box never has an
    empty feature the operator must discover. Returns (today-or-future
    dates ascending, doc_id for linking) — never raises.
    """
    try:
        from app.hooks.siyuan_tools import (
            _siyuan_request, get_notebook_id, siyuan_read_doc,
            siyuan_set_attrs, siyuan_upsert_page,
        )
        nb_id = get_notebook_id(LIFE_NOTEBOOK)
        if not nb_id:
            return [], None
        existing = _siyuan_request(
            "filetree/getIDsByHPath",
            {"notebook": nb_id, "path": KEY_DATES_PATH}, quiet=True,
        )
        ids = existing.get("data") or []
        if not ids:
            doc_id = siyuan_upsert_page(
                LIFE_NOTEBOOK, KEY_DATES_PATH, KEY_DATES_SEED)
            if doc_id:
                siyuan_set_attrs(doc_id, {"custom-type": "manual"})
                logger.info("Radar: seeded 📅 Key Dates page")
            ids = [doc_id] if doc_id else []
        if not ids:
            return [], None
        md = siyuan_read_doc(ids[0], full=True) or ""
        from app.graph.tools.capture import local_today
        today = local_today(now)      # operator-local calendar day, not the box's
        dates = []
        for m in _KEY_DATE_RE.finditer(md):
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d")
            except ValueError:
                continue
            if dt >= today:
                dates.append((dt, (m.group(2) or "(no label)").strip()))
        return sorted(dates), ids[0]
    except Exception as e:
        logger.warning(f"Radar: key dates unavailable: {e}")
        return [], None


def _utcnow() -> datetime:
    """Naive UTC, the convention of every timestamp in the stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _prep_lines(revisions, today: date) -> list[str]:
    """One line per talk worth a glance this morning: a delivery Sunday still ahead, nearest
    first, with the days to go; then revisions with no date, newest first. Delivered talks are silent."""
    from app.jobs.talk_nudge import delivery_date, is_delivered

    ahead, undated = [], []
    for rev in revisions:
        d = delivery_date(rev)
        if d is None:
            undated.append(rev)
        elif not is_delivered(rev, today):
            ahead.append((d, rev))

    def label(rev):
        talk = getattr(rev, "talk", None)
        return (getattr(talk, "theme", None) or getattr(talk, "name", None) or "") if talk else ""

    lines = [f"`{rev.version_name}` — {label(rev)} — {(d - today).days} days to go"
             for d, rev in sorted(ahead, key=lambda x: x[0])]
    lines += [f"`{rev.version_name}` — {label(rev)}" for rev in undated]
    return lines[:5]


def _prep_state() -> list[str]:
    try:
        from app.graph.tools.talkprep import _get_talkmaster_session
        from talkmaster.database import Revision
        session, engine = _get_talkmaster_session()
        try:
            cutoff = _utcnow() - timedelta(days=45)
            revs = (session.query(Revision).filter(Revision.timestamp >= cutoff)
                    .order_by(Revision.timestamp.desc()).limit(20).all())
            return _prep_lines(revs, date.today())
        finally:
            session.close()
            engine.dispose()
    except Exception as e:
        logger.warning(f"Radar: talkmaster unavailable: {e}")
        return []


def _flow_lint() -> list[str]:
    """Rows that would hijack their own dialog, dialogs that honour keywords: a tapped row
    is a message, so this is checked every morning against the live flows and triggers."""
    try:
        from app.graph.tools.flow_lint import live
        return live()
    except Exception as e:
        logger.warning(f"Radar: flow lint unavailable: {e}")
        return []


def _countdown(days: int) -> str:
    if days <= 0:
        return "**TODAY**"
    if days == 1:
        return "**TOMORROW**"
    return f"**in {days} days**"


def _render(now: datetime) -> tuple[str, list[str]]:
    """Return (markdown, notable_facts).

    notable_facts are stable identifiers of WHAT is notable (unit names,
    dates, labels) — the digest hash is computed over them, so rendered
    countdowns changing day to day never re-fire the email.
    """
    from app.graph.tools.capture import local_today, to_local
    speaker = os.getenv("TALK_SYNC_SPEAKER", "").strip().lower()
    talks = _upcoming_talks()
    events = _upcoming_events()
    failed = _failed_units()
    prep = _prep_state()
    key_dates, key_doc_id = _key_dates(now)

    facts = [f"failed:{u}" for u in sorted(failed)]
    lines = [
        f"> 🤖 Auto — last sweep: **{to_local(now):%Y-%m-%d %H:%M}**. "
        "Don't edit this page; it's rewritten every morning.",
        "",
        "## 🎤 Upcoming talks",
    ]
    if talks:
        for t in talks:
            who = t.get("speaker") or "?"
            mine = speaker and speaker in who.lower()
            when = t.get("week_of", "?")
            no = t.get("talk_number") or "—"
            flag = " ← **YOU**" if mine else ""
            lines.append(f"- {when} · No {no} · {who}{flag}")
            if mine:
                try:
                    dt = datetime.strptime(when, "%Y/%m/%d")
                    if dt - now <= timedelta(days=OWN_TALK_SOON_DAYS):
                        facts.append(f"own-talk:{when}:{no}")
                except ValueError:
                    pass
    else:
        lines.append("- *(unavailable — Organized isn't responding or has no data)*")

    lines += ["", "## ⭐ Key dates (yours)"]
    if key_dates:
        for dt, label in key_dates:
            days = (dt - local_today(now)).days
            lines.append(f"- {dt:%Y-%m-%d} · {label} — {_countdown(days)}")
            if days <= KEY_DATE_SOON_DAYS:
                facts.append(f"key-date:{dt:%Y-%m-%d}:{label}")
    else:
        lines.append("- *(no upcoming dates)*")
    if key_doc_id:
        lines.append(f'- ✍️ *edit the list in* (({key_doc_id} "📅 Key Dates"))')

    lines += ["", "## 📅 Events"]
    if events:
        for ev in events[:6]:
            start = str(ev.get("start") or ev.get("date") or "?")[:10]
            name = ev.get("name") or ev.get("title", "?")
            lines.append(f"- {start} · {name}")
            try:
                if (datetime.fromisoformat(start) - now
                        <= timedelta(days=EVENT_SOON_DAYS)):
                    facts.append(f"event:{start}:{name}")
            except ValueError:
                pass
    else:
        lines.append("- *(none, or unavailable)*")

    lines += ["", "## 🗣️ Talk prep (talkmaster)"]
    lines += [f"- {p}" for p in prep] or ["- *(nothing active)*"]

    lines += ["", "## 🚨 Services"]
    if failed:
        lines += [f"- ❌ **{u}** failed — `journalctl -u {u}`" for u in failed]
    else:
        lines.append("- ✅ all services healthy")

    lint = _flow_lint()
    facts += [f"flow-lint:{x}" for x in lint]
    lines += ["", "## 🧭 Flows (keyword clashes)"]
    if lint:
        lines += [f"- ⚠️ {x}" for x in lint]
    else:
        lines.append("- ✅ no row starts with a keyword; dialogs ignore triggers")

    # Confirmations: how often the line asked, and what the person answered. A tool that
    # is always answered Wi is a candidate for Toujou or the never tier — not notable,
    # never a digest, just read when it is there.
    try:
        from app.graph.tools.confirm import stats as _confirm_stats
        cstats = _confirm_stats()
    except Exception:
        cstats = {}
    if cstats:
        lines += ["", "## 🔐 Confirmations (since the counter started)"]
        for tool, c in sorted(cstats.items(), key=lambda kv: -kv[1].get("asked", 0)):
            hint = ""
            if c.get("asked", 0) >= 5 and not c.get("non"):
                hint = " ← always Wi: tap *Toujou* next time, or move it to the never tier"
            lines.append(f"- `{tool}`: asked {c.get('asked', 0)} · Wi {c.get('wi', 0)} · "
                         f"Non {c.get('non', 0)} · Toujou {c.get('toujou', 0)}{hint}")

    return "\n".join(lines) + "\n", facts


def _write_radar(md: str) -> str | None:
    """No-duplicate upsert: resolve by hpath first, update in place."""
    from app.hooks.siyuan_tools import (
        _siyuan_request, get_notebook_id, siyuan_set_attrs, siyuan_upsert_page,
    )
    nb_id = get_notebook_id(LIFE_NOTEBOOK)
    if not nb_id:
        logger.warning(f"Radar: notebook '{LIFE_NOTEBOOK}' unreachable — "
                       "skipping SiYuan write")
        return None
    existing = _siyuan_request(
        "filetree/getIDsByHPath",
        {"notebook": nb_id, "path": RADAR_PATH}, quiet=True,
    )
    ids = existing.get("data") or []
    if ids:
        _siyuan_request("block/updateBlock",
                        {"id": ids[0], "dataType": "markdown", "data": md})
        doc_id = ids[0]
    else:
        doc_id = siyuan_upsert_page(LIFE_NOTEBOOK, RADAR_PATH, md)
    if doc_id:
        siyuan_set_attrs(doc_id, {
            "custom-type": "auto",
            "custom-updated": datetime.now().isoformat(timespec="seconds"),
        })
    return doc_id


def main() -> None:
    now = datetime.now()
    md, facts = _render(now)
    notable = bool(facts)

    doc_id = _write_radar(md)

    # Edge-triggered digest: "your talk is in 9 days" is notable ONCE, not
    # nine mornings in a row. Email only when the SET of notable facts
    # changed since the last digest — countdown wording and non-notable
    # sections (prep list churn) never re-fire it.
    content_hash = hashlib.sha256(
        "\n".join(facts).encode()
    ).hexdigest()[:16]
    last = ""
    try:
        last = open(STATE_FILE).read().strip()
    except OSError:
        pass
    changed = content_hash != last

    from app.graph.tools.settings import is_snoozed
    snoozed = is_snoozed("radar", now)
    sent = False
    if notable and changed and not snoozed:
        from app.graph.tools.email import notify_admin
        sent = notify_admin(f"Radar — {now:%Y-%m-%d}", md[:3900])
        if sent:
            try:
                open(STATE_FILE, "w").write(content_hash)
            except OSError as e:
                logger.warning(f"Radar: cannot persist digest state: {e}")

    logger.info(
        f"Radar OK: doc={'written' if doc_id else 'SKIPPED'}, "
        f"notable={notable}, changed={changed}, "
        f"digest={'snoozed' if snoozed else ('sent' if sent else 'not sent')}"
    )

    # A due digest that could not be delivered must be LOUD, not a log
    # line: exit non-zero so OnFailure emails the operator through the
    # bridge (which does not ride the RapidPro API this send just failed
    # on). The doc write above already happened; the unsent hash means
    # tomorrow retries automatically. Found the hard way: a broken
    # gunicorn socket turned the morning digest into a silent no-op.
    # A snoozed digest is a deliberate no-send, not a failure — stay quiet.
    if notable and changed and not sent and not snoozed:
        logger.error("Radar: notable digest was due but could not be sent")
        sys.exit(1)


if __name__ == "__main__":
    main()
