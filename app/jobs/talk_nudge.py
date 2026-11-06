"""
Talk-prep nudges: the proactive half of the coaching loop.

Spaced practice needs scheduling, and a reactive coach can't schedule anyone. Every morning this
job looks at the recently active talk revisions and sends the admin ONE message (email by default,
see admin_notify_target) when something is due and missing. The cadence is keyed on the delivery
Sunday D (talkmaster.loop.cadence): drafts in every section by D-21; every section challenged once
and its debt answered by D-14; an evaluation by D-7; three voice notes between D-6 and D-1; the
debrief on D+1, and for a week of mornings after. A revision with no date keeps the older rule:
the thread, the drafts, the unanswered questions, a rehearsal gone stale.

Silent when everything is on track: a nudge that always fires trains the speaker to ignore it.
Never for a Sunday more than a week gone, never more than once a day (the timer), and the snooze
setting is honoured.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.talk_nudge
"""

import sys
from datetime import date, datetime, timedelta, timezone

from loguru import logger

ACTIVE_WINDOW_DAYS = 45     # revisions with any activity in this window
REHEARSAL_STALE_DAYS = 3
REHEARSAL_HORIZON_DAYS = 21  # only push rehearsal once delivery is this close (undated: always)
MAX_REVISIONS = 3           # never nudge about more than this many talks


def _utcnow() -> datetime:
    """Naive UTC, the convention of every timestamp in the stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def delivery_date(revision) -> date | None:
    """The Sunday the talk is given: the revision's own date, else the week tag in its name."""
    from talkmaster import loop

    return loop.delivery_date(revision)


def is_delivered(revision, today: date) -> bool:
    """A revision marked delivered, or whose Sunday has passed. The August 2026 nudge emailed ten
    mornings in a row about a talk already given; the debrief window is the one exception, and it
    is the only thing said then."""
    from talkmaster import loop

    return loop.is_delivered(revision, today)


def _die(msg: str) -> None:
    logger.error(f"Talk nudge ABORTED: {msg}")
    sys.exit(1)


# Recipient/channel selection lives in email.admin_notify_target(): email
# first (in-band Subject: lifted by the bridge), WhatsApp only when
# ADMIN_NOTIFY_CHANNEL=whatsapp.


def _facts(session, revision) -> dict:
    """What the store and the notebook say about a revision, for the cadence."""
    from talkmaster import loop
    from talkmaster.database import EvaluationScore, RehearsalRecord
    from talkmaster.repositories import get_root_node_for_revision
    from talkmaster.siyuan import coaching_log
    from talkmaster.siyuan.api import SiYuanAPI
    from talkmaster.siyuan.generation import _extract_speaker_draft

    undrafted, unchallenged, unanswered = [], [], 0
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
                if coaching_log.rounds(session, revision.id, node.text) == 0:
                    unchallenged.append(node.text)
                unanswered += len(coaching_log.unanswered(session, revision, node, siyuan))
    evaluated = (session.query(EvaluationScore).filter_by(revision_id=revision.id)
                 .filter((EvaluationScore.source.is_(None)) | (EvaluationScore.source != "self")).count()) > 0
    voice_notes = (session.query(RehearsalRecord).filter_by(revision_id=revision.id, kind=loop.VOICE).count())
    last_rehearsal = (session.query(RehearsalRecord).filter_by(revision_id=revision.id)
                      .filter((RehearsalRecord.kind.is_(None)) | (RehearsalRecord.kind != loop.CUE))
                      .order_by(RehearsalRecord.created_at.desc()).first())
    return {
        "thread": bool((revision.golden_thread or "").strip()),
        "undrafted": undrafted,
        "unchallenged": unchallenged,
        "unanswered": unanswered,
        "evaluated": evaluated,
        "voice_notes": voice_notes,
        "debriefed": loop.debrief_of(session, revision.id) is not None,
        "last_rehearsal": last_rehearsal,
    }


def _lines_for(keys: list[str], facts: dict, days: int | None) -> list[str]:
    """One line per due item, with the numbers behind it."""
    out = []
    for key in keys:
        if key == "thread":
            out.append("🧵 no central message yet: say it in one sentence, in your words")
        elif key == "drafts":
            out.append(f"✍️ {len(facts['undrafted'])} part(s) still without your draft")
        elif key == "challenge":
            bits = []
            if facts["unchallenged"]:
                bits.append(f"{len(facts['unchallenged'])} part(s) never challenged")
            if facts["unanswered"]:
                bits.append(f"{facts['unanswered']} question(s) awaiting your written answer")
            out.append("🥊 " + "; ".join(bits))
        elif key == "evaluate":
            out.append("📝 no evaluation yet: give your own score, then ask for the evaluation")
        elif key == "voice":
            out.append(f"🎤 {facts['voice_notes']} of 3 voice notes this week; record the next one")
        elif key == "debrief":
            out.append("📓 the talk is given: what landed, what did not, 1 to 5 on each focus point")
    return out


def _revision_report(session, revision, today: date | None = None) -> list[str]:
    """Actionable items for one revision (empty list = on track)."""
    from talkmaster import loop

    today = today or date.today()
    facts = _facts(session, revision)
    if loop.delivery_date(revision) is not None:
        return _lines_for(loop.cadence(revision, today, facts), facts, loop.days_to_go(revision, today))

    # Undated: the older rule.
    items: list[str] = []
    if not facts["thread"]:
        items.append("🧵 no golden thread set — state your central message first")
    if facts["undrafted"]:
        items.append(f"✍️ {len(facts['undrafted'])} section(s) still without your draft")
    if facts["unanswered"]:
        items.append(f"❓ {facts['unanswered']} coaching question(s) awaiting your written answer")
    last = facts["last_rehearsal"]
    if last is None:
        if not facts["undrafted"]:
            items.append("🎤 no rehearsal recorded yet — send a voice note")
    else:
        age = _utcnow() - last.created_at
        if age > timedelta(days=REHEARSAL_STALE_DAYS):
            items.append(f"🎤 last rehearsal was {age.days} day(s) ago")
    return items


def main() -> None:
    from app.graph.tools.email import notify_admin
    from app.graph.tools.talkprep import _get_talkmaster_session
    from talkmaster import loop
    from talkmaster.database import Revision

    session, engine = _get_talkmaster_session()
    try:
        cutoff = _utcnow() - timedelta(days=ACTIVE_WINDOW_DAYS)
        today = date.today()
        revisions = (
            session.query(Revision)
            .filter(Revision.timestamp >= cutoff)
            .order_by(Revision.timestamp.desc())
            .limit(MAX_REVISIONS * 2)
            .all()
        )
        # A Sunday more than a week gone is silent; the week after it, only the debrief speaks.
        keep = []
        for r in revisions:
            days = loop.days_to_go(r, today)
            if days is not None and days < -loop.DEBRIEF_WINDOW_DAYS:
                continue
            if days is None and loop.is_delivered(r, today):
                continue
            keep.append(r)
        revisions = keep[:MAX_REVISIONS]

        if not revisions:
            logger.info("Talk nudge: no active revisions — nothing to say")
            return

        from app.graph.tools.talkprep import _clean_title, talk_handle

        sections = []
        for rev in revisions:
            try:
                items = _revision_report(session, rev, today)
            except Exception as e:
                logger.warning(f"Talk nudge: skipping '{rev.version_name}': {e}")
                continue
            if items:
                bullet = "\n".join(f"  • {i}" for i in items)
                handle = talk_handle(rev.talk)
                days = loop.days_to_go(rev, today)
                when = (f" — Sunday {loop.delivery_date(rev).isoformat()}, {days} days to go" if days is not None and days >= 0
                        else " — given" if days is not None else "")
                header = f"*Talk {handle}* — {_clean_title(rev.talk.name)}{when}"
                sections.append(f"{header}\n{bullet}")

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
            + "\n\nChoose the talk by its number, then the step named above."
        )
        if not notify_admin("Talk prep this morning", text[:3900]):
            _die("talk-prep nudge not delivered")
        logger.info(f"Talk nudge sent ({len(sections)} revision(s) flagged)")
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    main()
