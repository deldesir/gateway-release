"""
🐑 Shepherd nudge — a weekly, roster-aware prompt to keep your people in view.

Crosses your field-service group (Organized's roster, the same source
person_sync uses) with what you've actually logged, and nudges on the gap:
group members you haven't jotted a touch for, or have gone quiet on. This is
the pump that turns the 182-person roster from a one-way mirror into a reason
to capture — so the shepherd view (built purely from `person:` captures) has
something to work with.

Delivered on the operator's notify_channel (EMAIL by default — a weekly coverage
list is report-like, not a quick ping; flip with `set notify_channel whatsapp`).
Run-once-per-ISO-week (SHEPHERD_FORCE=1 re-sends); nothing to say — no group
resolved, snoozed, or everyone already current — sends nothing and exits 0. Loud
exit only if a due nudge can't be delivered → OnFailure emails the operator.

Point it at your group once: "set field_group Gwoup 13" (else it auto-detects
from your own roster record). Snooze from chat: "mute shepherd until 2026-09-01".

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.shepherd_nudge
"""

import json
import os
import sys
from datetime import datetime

from loguru import logger

STATE_FILE = os.getenv(
    "SHEPHERD_STATE_FILE", "/opt/iiab/ai-gateway/data/shepherd_state.json"
)


def _die(msg: str) -> None:
    logger.error(f"Shepherd nudge ABORTED: {msg}")
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
        logger.warning(f"Shepherd nudge: cannot persist state: {e}")


def main() -> None:
    from app.graph.tools.people import group_coverage, group_coverage_lines
    from app.graph.tools.settings import is_snoozed

    now = datetime.now()
    iso = now.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    force = os.getenv("SHEPHERD_FORCE", "") == "1"

    state = _load_state()
    if state.get("week") == week_key and state.get("sent") and not force:
        logger.info(f"Shepherd nudge: {week_key} already sent — skipping "
                    "(SHEPHERD_FORCE=1 to re-send)")
        return

    if is_snoozed("shepherd", now):
        logger.info("Shepherd nudge: snoozed — nothing sent")
        _save_state({"week": week_key, "sent": False})
        return

    cov = group_coverage(now)
    if not cov["label"] or cov["total"] == 0:
        logger.info("Shepherd nudge: no field group resolved — nothing to send "
                    "(set field_group, or enable Organized Data sharing)")
        _save_state({"week": week_key, "sent": False})
        return

    # Only nudge on an actual gap; if everyone's current, stay silent (but mark
    # the week done so a fully-covered week doesn't re-check every day).
    neglected = len(cov["missing"]) + len(cov["stale"])
    if neglected == 0:
        logger.info("Shepherd nudge: group fully covered — nothing to nudge")
        _save_state({"week": week_key, "sent": True})
        return

    lines = group_coverage_lines(now, limit=8)
    body = ("🐑 *Shepherd check* — your field group this week:\n\n"
            + "\n".join(lines)
            + "\n\n_Jot one:_ `person: Jean | encouraged, back at meetings`")

    from app.graph.tools.email import notify_admin
    # A weekly coverage LIST is report-like, not a quick ping — so it follows
    # the operator's notify_channel (email by default), like the sitrep and
    # month-end, NOT the WhatsApp habit channel used by reminders/spiritual.
    if not notify_admin("Shepherd — your field group this week", body):
        _die("shepherd nudge not delivered")

    _save_state({"week": week_key, "sent": True})
    logger.info(f"Shepherd nudge: sent ({neglected} to reach in {cov['label']})")


if __name__ == "__main__":
    main()
