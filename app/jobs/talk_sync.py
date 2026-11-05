"""
Organized → talkmaster assignment sync: the scheduling half of the loop.

The Organized app knows WHEN a talk happens and WHO gives it; talkmaster knows
HOW to prepare it. This job bridges them: every night it reads the upcoming
weekend public talks from Organized (get_upcoming_talks joins sources' talk
number + schedules' speaker + persons' names by week) and, for each talk the
configured speaker delivers, ensures a talkmaster revision exists — imported
from the jwlinker S-34 corpus and named `s34-<no>-w<YYYY-MM-DD>` so the
delivery week is recoverable from the revision itself (talk_nudge picks the
revision up through its normal recent-activity window).

One WhatsApp line is sent per NEWLY created revision so the speaker knows prep
has started; an already-synced schedule stays silent.

Config (gateway .env):
  TALK_SYNC_SPEAKER   substring of the speaker's display name in Organized
                      (e.g. a first name). REQUIRED — without it the job
                      cannot know whose talks to prepare and aborts loudly.
  TALK_SYNC_LANGUAGE  revision language (default 'ht').
  TALK_SYNC_PUB       jwlinker publication code (default 's34').

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.talk_sync
"""

import json
import os
import sys

from loguru import logger

MAX_WEEKS = 8


def _die(msg: str) -> None:
    logger.error(f"Talk sync ABORTED: {msg}")
    sys.exit(1)


# Recipient/channel selection lives in email.admin_notify_target(): email
# first (in-band Subject: lifted by the bridge), WhatsApp only when
# ADMIN_NOTIFY_CHANNEL=whatsapp.


def _fetch_upcoming_talks() -> list[dict]:
    from app.graph.tools.organized import query_organized_api

    raw = query_organized_api({"action": "get_upcoming_talks", "weeks": MAX_WEEKS})
    data = json.loads(raw)
    if "error" in data:
        _die(f"Organized query failed: {data['error']}")
    return data.get("talks", [])


def _version_name(talk_number, week_of: str) -> str:
    return f"s34-{talk_number}-w{week_of.replace('/', '-')}"


# Language code -> jwlinker MepsLanguageIndex (Publications.language).
# The corpus holds S-34 in these four; extend if more are ingested.
MEPS_LANG = {"ht": "51", "en": "0", "fr": "3", "es": "1"}


def _ensure_talk(session, talk_number, pub_code: str, language: str):
    """Return the talkmaster Talk for this S-34 number, importing if absent.

    The jwlinker corpus holds two topic-name formats ('102. Fikse atansyon…'
    and 'No 78 Sèvi Jewova…' from different ingestion batches), so both the
    existing-talk lookup and the corpus search try both prefixes.
    """
    from talkmaster.database import Talk
    from talkmaster.bridge import import_from_jwlinker, save_imported_talk

    prefixes = (f"{talk_number}. ", f"No {talk_number} ")
    for p in prefixes:
        existing = session.query(Talk).filter(Talk.name.like(f"{p}%")).first()
        if existing:
            return existing

    talk = None
    last_error = None
    for p in prefixes:
        try:
            talk = import_from_jwlinker(
                pub_code=pub_code,
                topic_name=p.strip(),
                talk_theme="",  # bridge falls back to the topic name
                language=language,
                lang_id=MEPS_LANG.get(language),
            )
            break
        except ValueError as e:
            last_error = e
    if talk is None:
        raise last_error or ValueError(f"talk {talk_number} not in corpus")

    talk_id = save_imported_talk(talk)
    logger.info(f"Imported talk No {talk_number} from {pub_code} (id={talk_id})")
    return session.query(Talk).filter_by(id=talk_id).first()


def _notify(lines: list[str]) -> None:
    from app.graph.tools.email import notify_admin

    text = "🎤 *Talk prep started automatically:*\n" + "\n".join(lines)
    if notify_admin("Talk prep started automatically", text):
        logger.info("Talk sync: notified admin")
    else:
        logger.warning("Talk sync: notify not delivered")


def main() -> None:
    speaker_match = os.getenv("TALK_SYNC_SPEAKER", "").strip().lower()
    if not speaker_match:
        _die("TALK_SYNC_SPEAKER is not set — configure the speaker name to sync")

    language = (os.getenv("TALK_SYNC_LANGUAGE") or "ht").strip().lower()[:2]
    pub_code = (os.getenv("TALK_SYNC_PUB") or "s34").strip().lower()

    talks = _fetch_upcoming_talks()
    mine = [
        t for t in talks
        if speaker_match in (t.get("speaker") or "").lower()
    ]
    # An outline number of 0/None means fsr couldn't resolve the docx title
    # against the S-34 index. Silently filtering those would mean YOUR talk
    # never gets prepared and nobody notices — warn loudly instead.
    for t in [t for t in mine if not t.get("talk_number")]:
        logger.warning(
            f"Talk sync: upcoming talk for '{t.get('speaker')}' "
            f"(week {t.get('week_of')}) has NO outline number — the title "
            f"likely failed to resolve in fsr; re-export or fix the title. "
            f"This talk will NOT be prepared."
        )
    mine = [t for t in mine if t.get("talk_number")]
    if not mine:
        logger.info(
            f"Talk sync: no upcoming talks for '{speaker_match}' "
            f"({len(talks)} scheduled overall) — nothing to do"
        )
        return

    from app.graph.tools.talkprep import _get_talkmaster_session, create_revision
    from talkmaster.database import Revision

    session, engine = _get_talkmaster_session()
    created: list[str] = []
    try:
        for t in mine:
            week_of = t["week_of"]
            number = t["talk_number"]
            version = _version_name(number, week_of)

            if session.query(Revision).filter_by(version_name=version).first():
                logger.info(f"Talk sync: revision '{version}' already exists — skip")
                continue

            try:
                talk = _ensure_talk(session, number, pub_code, language)
            except Exception as e:
                logger.warning(f"Talk sync: cannot import talk No {number}: {e}")
                continue
            if talk is None:
                logger.warning(f"Talk sync: talk No {number} not found in corpus")
                continue

            # Reuse the full tool path — persona, canvases, SiYuan structure.
            result = create_revision({
                "version_name": version,
                "audience_description": "Local congregation audience",
                "language": language,
                "active_talk_id": str(talk.id),
            })
            if not result.startswith("✅"):
                logger.warning(f"Talk sync: create_revision('{version}'): {result}")
                continue

            logger.info(f"Talk sync: created revision '{version}' for {week_of}")
            created.append(f"• week {week_of} — *No {number}* ({version})")
    finally:
        session.close()

    if created:
        _notify(created)
        logger.info(f"Talk sync: {len(created)} revision(s) created")
    else:
        logger.info("Talk sync: everything already in sync")


if __name__ == "__main__":
    main()
