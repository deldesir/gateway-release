"""
🕊️ Spiritual nudge — one gentle morning ping for the things that come first.

Combines (all nudge-only, no content reproduced):
  - the daily-text reminder (read it in JW Library),
  - a meeting-prep nudge on a configured meeting day,
  - a Monday study-rhythm reflection.

Delivered on WhatsApp — the habit channel, same as reminders (email-first is
for scarce reports, not a daily ping you asked for). Run-once-per-day guard
(SPIRITUAL_FORCE=1 re-sends); a nudge that's genuinely empty or fully snoozed
sends nothing and exits 0. Loud exit only if a due nudge can't be delivered
→ OnFailure emails the operator through the bridge.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.spiritual_nudge
"""

import json
import os
import sys
from datetime import datetime

from loguru import logger

STATE_FILE = os.getenv(
    "SPIRITUAL_STATE_FILE", "/opt/iiab/ai-gateway/data/spiritual_state.json"
)


def _die(msg: str) -> None:
    logger.error(f"Spiritual nudge ABORTED: {msg}")
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
        logger.warning(f"Spiritual nudge: cannot persist state: {e}")


def main() -> None:
    from app.graph.tools.spiritual import build_nudge

    now = datetime.now()
    today_key = f"{now:%Y-%m-%d}"
    force = os.getenv("SPIRITUAL_FORCE", "") == "1"

    state = _load_state()
    if state.get("date") == today_key and state.get("sent") and not force:
        logger.info("Spiritual nudge: already sent today — skipping "
                    "(SPIRITUAL_FORCE=1 to re-send)")
        return

    body = build_nudge(now)
    if not body:
        logger.info("Spiritual nudge: nothing to say (empty or snoozed)")
        _save_state({"date": today_key, "sent": False})
        return

    from app.graph.tools.email import notify_admin
    # WhatsApp — the on-phone habit channel, right where JW Library lives.
    if not notify_admin("Morning", body, channel="whatsapp"):
        _die("spiritual nudge not delivered")

    _save_state({"date": today_key, "sent": True})
    logger.info("Spiritual nudge: sent")


if __name__ == "__main__":
    main()
