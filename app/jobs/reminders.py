"""
⏰ Reminders — fire the operator's due reminders. Runs every ~10 min.

Reads captures.jsonl for kind='reminder' whose parsed due time (the
operator's local tz, NOT the box clock) has arrived and that have not yet
fired (kind='reminder_fired' markers), sends ONE nudge through the admin
bridge (email-first, same channel as the other jobs), and marks them fired
so they never double-send.

No run-once-per-day guard: each run fires whatever is newly due; the
fired-state marker (not a timestamp) is the idempotency key, so a missed run,
a Persistent=true catch-up, or a manual re-run never re-sends a reminder.

Loud exit only if a due reminder could NOT be delivered — OnFailure emails
the operator through the bridge, and the un-marked reminder retries on the
next run automatically.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.reminders
"""

import sys

from loguru import logger


def _die(msg: str) -> None:
    logger.error(f"Reminders ABORTED: {msg}")
    sys.exit(1)


def main() -> None:
    from app.graph.tools.capture import (
        due_reminders, mark_reminders_fired, _local_now,
    )

    now = _local_now()
    due = due_reminders(now)
    if not due:
        logger.debug("Reminders: none due")  # runs every couple min — stay quiet
        return

    if len(due) == 1:
        body = f"⏰ Reminder: {due[0].get('text', '')}"
    else:
        lines = "\n".join(f"• {d.get('text', '')}" for d in due)
        body = f"⏰ {len(due)} reminders due:\n{lines}"

    from app.graph.tools.email import notify_admin
    from app.graph.tools.settings import get_setting

    # Reminders default to WhatsApp — the operator set them there and lives
    # there; email-first is for the scarce scheduled reports, not a ping you
    # asked for. Tune from chat: "send reminders by email" (reminder_channel).
    channel = get_setting("reminder_channel")
    if not notify_admin("Reminder", body, channel=channel):
        # leave them unfired so the next run retries; be loud for OnFailure
        _die("reminder nudge not delivered")

    mark_reminders_fired([d.get("id") for d in due])
    logger.info(f"Reminders: fired {len(due)} "
                f"({', '.join(d.get('id', '?') for d in due)})")


if __name__ == "__main__":
    main()
