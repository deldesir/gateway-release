"""
Talk-prep nudges: the proactive half of the coaching loop.

Spaced practice needs scheduling, and a reactive coach can't schedule anyone.
Every morning this job inspects recently-active talk revisions and sends the
admin ONE message (email by default — see admin_notify_target) when there is
something actionable:

  - sections still without a speaker draft
  - Socratic questions left unanswered in the canvases
  - no rehearsal in the last 3+ days
  - a golden thread still unset

Silent when everything is on track — a nudge that always fires trains the
speaker to ignore it.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.talk_nudge
"""

import os
import re
import sys
from datetime import date, datetime, timedelta

from loguru import logger

ACTIVE_WINDOW_DAYS = 45     # revisions with any activity in this window
REHEARSAL_STALE_DAYS = 3
REHEARSAL_HORIZON_DAYS = 21  # only push rehearsal once delivery is this close
MAX_REVISIONS = 3           # never nudge about more than this many talks

_WEEK_TAG = re.compile(r"-w(\d{4}-\d{2}-\d{2})$")


def delivery_date(revision) -> date | None:
    """Sunday the talk is given, from talksync's `s34-50-w2026-08-03` naming
    (week_of = Monday → +6). None when the revision isn't schedule-bound."""
    m = _WEEK_TAG.search(getattr(revision, "version_name", "") or "")
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1)) + timedelta(days=6)
    except ValueError:
        return None


def is_delivered(revision, today: date) -> bool:
    """A revision whose delivery Sunday has passed must never be nudged — the
    August 2026 nudge emailed 10 mornings in a row about a talk already given."""
    d = delivery_date(revision)
    return d is not None and d < today


def _die(msg: str) -> None:
    logger.error(f"Talk nudge ABORTED: {msg}")
    sys.exit(1)


# Recipient/channel selection lives in email.admin_notify_target(): email
# first (in-band Subject: lifted by the bridge), WhatsApp only when
# ADMIN_NOTIFY_CHANNEL=whatsapp.


def _revision_report(session, revision) -> list[str]:
    """Actionable items for one revision (empty list = on track)."""
    from talkmaster.database import RehearsalRecord
    from talkmaster.siyuan.api import SiYuanAPI
    from talkmaster.siyuan.generation import (
        _extract_speaker_draft, _unanswered_challenge_items,
    )
    from talkmaster.repositories import get_root_node_for_revision

    items: list[str] = []

    if not revision.golden_thread:
        items.append("🧵 no golden thread set — state your central message first")

    undrafted, unanswered = [], 0
    root = get_root_node_for_revision(session, revision.id)
    if root:
        section_nodes = sorted(
            (n for n in root.children if n.node_type == "section"),
            key=lambda n: n.sequence,
        )
        with SiYuanAPI() as siyuan:
            for node in section_nodes:
                if not node.siyuan_doc_id:
                    continue
                canvas = siyuan.get_doc_content_markdown(node.siyuan_doc_id) or ""
                if not _extract_speaker_draft(canvas):
                    undrafted.append(node.text)
                unanswered += len(_unanswered_challenge_items(canvas))
    if undrafted:
        items.append(f"✍️ {len(undrafted)} section(s) still without your draft")
    if unanswered:
        items.append(f"❓ {unanswered} coaching question(s) awaiting your written answer")

    last_rehearsal = (
        session.query(RehearsalRecord)
        .filter_by(revision_id=revision.id)
        .order_by(RehearsalRecord.created_at.desc())
        .first()
    )
    deliver = delivery_date(revision)
    near = deliver is None or (deliver - date.today()) <= timedelta(days=REHEARSAL_HORIZON_DAYS)
    if last_rehearsal is None:
        # Only nag about rehearsal once there is something to rehearse AND the
        # date is close enough for practice to matter
        if not undrafted and near:
            items.append("🎤 no rehearsal recorded yet — time it (`rehearse`)")
    elif near:
        age = datetime.utcnow() - last_rehearsal.created_at
        if age > timedelta(days=REHEARSAL_STALE_DAYS):
            items.append(f"🎤 last rehearsal was {age.days} day(s) ago")
    return items


def main() -> None:
    from app.graph.tools.email import notify_admin
    from app.graph.tools.talkprep import _get_talkmaster_session
    from talkmaster.database import Revision

    session, engine = _get_talkmaster_session()
    try:
        cutoff = datetime.utcnow() - timedelta(days=ACTIVE_WINDOW_DAYS)
        revisions = (
            session.query(Revision)
            .filter(Revision.timestamp >= cutoff)
            .order_by(Revision.timestamp.desc())
            .limit(MAX_REVISIONS)
            .all()
        )
        today = date.today()
        skipped = [r.version_name for r in revisions if is_delivered(r, today)]
        revisions = [r for r in revisions if not is_delivered(r, today)]
        if skipped:
            logger.info(f"Talk nudge: skipping delivered revision(s): {', '.join(skipped)}")

        if not revisions:
            logger.info("Talk nudge: no active revisions — nothing to say")
            return

        from app.graph.tools.talkprep import _clean_title, talk_handle

        sections = []
        for rev in revisions:
            try:
                items = _revision_report(session, rev)
            except Exception as e:
                logger.warning(f"Talk nudge: skipping '{rev.version_name}': {e}")
                continue
            if items:
                bullet = "\n".join(f"  • {i}" for i in items)
                handle = talk_handle(rev.talk)
                header = f"*Talk {handle}* — {_clean_title(rev.talk.name)}"
                start = f"  ▶️ Start with `use talk {handle}`"
                sections.append(f"{header}\n{start}\n{bullet}")

        if not sections:
            logger.info("Talk nudge: all revisions on track — staying silent")
            return

        from app.graph.tools.settings import is_snoozed
        if is_snoozed("talk_nudge"):
            logger.info("Talk nudge: snoozed — staying silent")
            return

        text = (
            "🌅 *Talk prep this morning:*\n\n"
            + "\n\n".join(sections)
            + "\n\nAfter `use talk <number>` you can: `set golden thread …`, "
            "`challenge section <name>`, or `my talks` to see progress."
        )
        if not notify_admin("Talk prep this morning", text[:3900]):
            _die("talk-prep nudge not delivered")
        logger.info(f"Talk nudge sent ({len(sections)} revision(s) flagged)")
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    main()
