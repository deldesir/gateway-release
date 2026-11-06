"""The loop's verbs: prepare, set focus, draft, answer, model, rate, debrief, mark mastered, my
progress. Every one of them opens a gate only with the record the gate is named for (D-012), and
every line a person reads is in talkprep_text. The nightly sync starts a preparation through the
same function the chat verb uses, so a talk prepared by hand and a talk from the schedule are the
same thing in the store.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from app.graph.tools import _talk_context as _tctx
from app.graph.tools import talkprep as _tp
from app.graph.tools.talkprep_text import lang_of as _lang_of
from app.graph.tools.talkprep_text import t as _t

logger = logging.getLogger(__name__)


def _rev(session, args):
    """The active revision row, or (None, reply)."""
    from talkmaster.database import Revision

    name = _tctx.active_revision(args) or ""
    if not name:
        return None, _t("no_active_revision", args)
    rev = session.query(Revision).filter_by(version_name=name).first()
    if not rev:
        return None, _t("rev_not_found", args, name=name)
    return rev, None


# The helpers of talkprep are reached through the module, never bound here by name: what the
# gateway (or a test) sets on that module at call time is what runs.


# ── Start: a talk and its Sunday ──────────────────────────────────────────────


CREATED, EXISTS, FAILED = "created", "exists", "failed"


def prepare_revision(session, talk, sunday: date, language: str, user_id=None,
                     audience: str = "Local congregation audience") -> tuple[str, str]:
    """Start a preparation for a talk on a Sunday: the revision named by its week, dated, in the
    speaker's language, with its canvases. Returns (CREATED, name), (EXISTS, name) when the week's
    revision was already there (it becomes the active one), or (FAILED, reply) with the tool's own
    words when nothing could be made."""
    from talkmaster import loop, standard
    from talkmaster.database import Revision

    version = loop.version_name(_tp.talk_handle(talk), sunday)
    existing = session.query(Revision).filter_by(version_name=version).first()
    if existing:
        if existing.delivery_date is None:
            existing.delivery_date = sunday
            session.commit()
        _tctx.set_talk(user_id, talk.id)
        _tctx.set_revision(user_id, version)
        return EXISTS, version
    reply = _tp.create_revision({
        "version_name": version,
        "audience_description": audience,
        "language": language,
        "active_talk_id": str(talk.id),
        "user_id": user_id,
        "delivery_date": sunday.isoformat(),
    })
    if not reply.startswith("✅"):
        return FAILED, reply
    row = session.query(Revision).filter_by(version_name=version).first()
    if row is not None:
        row.delivery_date = sunday
        row.status = loop.PREPARING
        row.rubric_version = standard.version()
        session.commit()
    return CREATED, version


def prepare_talk(args: dict, **kwargs) -> str:
    """Start preparing a talk for a Sunday: `prepare talk 75 for 2026-12-27`.

    Args:
        talk: The talk's number, as it is on the outline.
        sunday: The day the talk is given (a date; it is moved to its Sunday).
        language: Optional, the revision's language (ht, en, fr).
    """
    from talkmaster import loop

    query = str(args.get("talk") or args.get("talk_id") or "").strip()
    when = loop.parse_date(args.get("sunday") or args.get("date") or args.get("when"))
    if not query:
        return _t("select_which", args)
    if when is None:
        return _t("prepare_ask_date", args)
    sunday = loop.sunday_of(when)
    language = (args.get("language") or "ht").strip().lower()[:2]

    def _sync():
        session, engine = _tp._get_talkmaster_session()
        try:
            talk, msg = _tp._resolve_talk(session, query)
            if not talk and query.isdigit():
                from app.jobs.talk_sync import _ensure_talk
                try:
                    talk = _ensure_talk(session, query, "s34", language)
                except Exception as e:  # the outline is not in the corpus
                    logger.warning(f"prepare_talk: talk {query} not importable: {e}")
                    talk = None
            if not talk:
                return msg or _t("select_unknown", args, query=query)
            state, value = prepare_revision(session, talk, sunday, language, user_id=args.get("user_id"))
            if state == EXISTS:
                return _t("prepare_exists", args, name=value, handle=_tp.talk_handle(talk), date=sunday.isoformat())
            if state == FAILED:
                return value
            days = (sunday - date.today()).days
            return _t("prepare_ok", args, handle=_tp.talk_handle(talk), title=_tp._clean_title(talk.name),
                      date=sunday.isoformat(), days=days, name=value)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"prepare_talk failed: {e}")
        return _t("error", args)


# ── Anchor: the focus ──────────────────────────────────────────────────────────


def set_focus(args: dict, **kwargs) -> str:
    """Choose one or two study points to work on for this talk: `set focus 8 12`, or `8.45` to
    sharpen a point to one textbook sub-criterion. The system proposes a default from the last
    debrief; the speaker chooses.

    Args:
        points: The study points, e.g. "8 12" or "8.45".
    """
    from talkmaster import loop

    raw = str(args.get("points") or args.get("focus") or "").strip()

    def _sync():
        session, engine = _tp._get_talkmaster_session()
        try:
            rev, reply = _rev(session, args)
            if reply:
                return reply
            lang = _lang_of(args)
            if not raw:
                proposed = loop.proposed_focus(session)
                if proposed:
                    return _t("focus_propose", args, points="; ".join(loop.focus_names(proposed, lang)))
                return _t("focus_ask", args)
            try:
                points = loop.parse_focus(raw)
            except ValueError as e:
                return _t("focus_bad", args, reason=str(e))
            loop.set_focus(rev, points)
            session.commit()
            leads = loop.focus_leads(points, rev.language)
            return _t("focus_set", args, name=rev.version_name,
                      points="; ".join(loop.focus_names(points, lang)), leads=leads)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"set_focus failed: {e}")
        return _t("error", args)


# ── Draft and answer, from the phone ───────────────────────────────────────────


def draft_section(args: dict, **kwargs) -> str:
    """Add words to your own draft of a part, from the phone: they land in the ✍️ area of the
    part's canvas, after what is there.

    Args:
        section_title: The part, by number or by a word of its title.
        text: What to add.
    """
    section = _tctx.active_section(args) or ""
    body = str(args.get("text") or "").strip()
    if not body:
        return _t("draft_ask", args)
    if not section:
        return _t("section_ask", args)

    def _sync():
        from talkmaster.siyuan import generation
        from talkmaster.siyuan.api import SiYuanAPI

        session, engine = _tp._get_talkmaster_session()
        try:
            rev, reply = _rev(session, args)
            if reply:
                return reply
            node = generation.resolve_section_node(session, rev, section)
            if not node.siyuan_doc_id:
                return _t("rev_canvases_deferred", args)
            with SiYuanAPI() as siyuan:
                siyuan.append_block(node.siyuan_doc_id, body)
            _tctx.set_section(args.get("user_id"), node.text)
            drafted, total = _tp._draft_progress(session, rev)
            return _t("draft_ok", args, section=node.text, words=len(body.split()),
                      drafted=drafted if drafted is not None else "?", total=total)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        from talkmaster.siyuan.generation import SectionNotFoundError
        if isinstance(e, SectionNotFoundError):
            return f"🤔 {e}"
        logger.error(f"draft_section failed: {e}")
        return _t("error", args)


def answer_challenge(args: dict, **kwargs) -> str:
    """Answer one open question of the last challenge on a part, in writing: `answer 2 <text>`.
    The answer goes into its slot in the part's coaching log; the debt shrinks.

    Args:
        section_title: The part.
        n: Which open question (1 is the first still blank).
        text: Your answer.
    """
    section = _tctx.active_section(args) or ""
    body = str(args.get("text") or "").strip()
    raw_n = args.get("n") or args.get("number")
    if raw_n in (None, "") and body:
        m = re.match(r"^(\d{1,2})\s+(.*)$", body, re.S)  # `answer 2 my words`: the number leads the text
        if m:
            raw_n, body = m.group(1), m.group(2).strip()
    try:
        n = int(raw_n or 1)
    except (TypeError, ValueError):
        n = 1
    if not body:
        return _t("answer_ask", args)
    if not section:
        return _t("section_ask", args)

    def _sync():
        from talkmaster.siyuan import coaching_log, generation
        from talkmaster.siyuan.api import SiYuanAPI

        session, engine = _tp._get_talkmaster_session()
        try:
            rev, reply = _rev(session, args)
            if reply:
                return reply
            node = generation.resolve_section_node(session, rev, section)
            with SiYuanAPI() as siyuan:
                done, left = coaching_log.answer_slot(session, rev, node, n, body, siyuan)
            if not done:
                if left == 0:
                    return _t("answer_none_open", args, section=node.text)
                return _t("answer_which", args, section=node.text, left=left)
            if left == 0:
                return _t("answer_ok_clear", args, section=node.text)
            return _t("answer_ok", args, section=node.text, left=left)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        from talkmaster.siyuan.generation import SectionNotFoundError
        if isinstance(e, SectionNotFoundError):
            return f"🤔 {e}"
        logger.error(f"answer_challenge failed: {e}")
        return _t("error", args)


# ── The model text, last ───────────────────────────────────────────────────────


def model_section(args: dict, **kwargs) -> str:
    """The model text for a part, for comparison and never for delivery. It unlocks after your
    own draft and one challenge round with every question answered, and it goes to the part's
    coaching log.

    Args:
        section_title: The part.
    """
    section = _tctx.active_section(args) or ""
    revision_name = _tctx.active_revision(args) or ""
    if not revision_name:
        return _t("no_active_revision", args)
    if not section:
        return _t("section_ask", args)

    def _sync():
        from talkmaster.siyuan import generation

        session, engine = _tp._get_talkmaster_session()
        try:
            rev, reply = _rev(session, args)
            if reply:
                return reply
            result = generation.model_section(version_name=rev.version_name, db=session, section_title=section)
            if result:
                return _t("model_ok", args, section=section)
            return _t("develop_failed", args, section=section)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        from talkmaster.siyuan.generation import ModelGateError, SectionNotFoundError
        if isinstance(e, SectionNotFoundError):
            return f"🤔 {e}"
        if isinstance(e, ModelGateError):
            return _t(f"model_gate_{e.reason}", args, section=section)
        logger.error(f"model_section failed: {e}")
        return _t("error", args)


# ── Rehearse: the speaker rates what only the room can hear ───────────────────


def self_rate(args: dict, **kwargs) -> str:
    """Rate your own delivery after a rehearsal, 1 to 5 a point: `rate 8:4 13:3`. A study point
    (1 to 20) is rated on the point; a textbook point of the self-check (2, 3, 4, 8, 12, 13, 15,
    16, 17, 29) on its own row. Nothing the assistant cannot hear is scored by the assistant.

    Args:
        ratings: Pairs like "8:4 13:3".
    """
    from talkmaster import loop, standard

    ratings = loop.parse_ratings(args.get("ratings") or args.get("text") or "")
    if not ratings:
        return _t("rate_ask", args)

    def _sync():
        import json as _json

        from talkmaster.database import EvaluationScore, RehearsalRecord

        session, engine = _tp._get_talkmaster_session()
        try:
            rev, reply = _rev(session, args)
            if reply:
                return reply
            last = (session.query(RehearsalRecord).filter_by(revision_id=rev.id)
                    .order_by(RehearsalRecord.created_at.desc(), RehearsalRecord.id.desc()).first())
            lang = _lang_of(args)
            recorded, unknown = [], []
            for n, value in ratings.items():
                if standard.study_point(n) is not None:
                    row = standard.point_row(session, standard.STANDARD, n)
                    label = standard.name(n, lang)
                elif n in standard.self_check():
                    row = standard.point_row(session, standard.TEXTBOOK, n)
                    label = f"#{n} {standard.textbook_points()[n]['name']}"
                else:
                    unknown.append(str(n))
                    continue
                session.add(EvaluationScore(
                    revision_id=rev.id, evaluation_point_id=row.id, score=value, source="self",
                    feedback="self-rated after a rehearsal",
                    detail_json=_json.dumps({"source": "self", "rehearsal_id": last.id if last else None}),
                ))
                recorded.append(f"{label}: {value}/5")
            session.commit()
            lines = [_t("rate_ok", args, n=len(recorded))] + [f"• {r}" for r in recorded]
            if unknown:
                lines.append(_t("rate_unknown", args, points=", ".join(unknown)))
            return "\n".join(lines)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"self_rate failed: {e}")
        return _t("error", args)


# ── After the talk ─────────────────────────────────────────────────────────────


def debrief_talk(args: dict, **kwargs) -> str:
    """The three answers after the talk: what landed, what did not, and 1 to 5 on each focus
    point. Only once the Sunday has come; it marks the talk delivered.

    Args:
        landed: What landed.
        did_not: What did not.
        ratings: Optional, "8:4 14:2" on the focus points.
    """
    from talkmaster import loop

    landed = str(args.get("landed") or "").strip()
    did_not = str(args.get("did_not") or args.get("didnt") or "").strip()
    ratings_raw = str(args.get("ratings") or "")
    text = str(args.get("text") or "").strip()
    if text and not landed:
        # the one-line form of chat: what landed | what did not | 8=4 14=2
        parts = [p.strip() for p in re.split(r"\s*[|;]\s*", text)]
        landed = parts[0] if parts else ""
        did_not = parts[1] if len(parts) > 1 else ""
        if len(parts) > 2:
            ratings_raw = parts[2]
        elif did_not and loop.parse_ratings(did_not) and not re.search(r"[A-Za-z]{3,}", did_not):
            did_not, ratings_raw = "", did_not
    ratings = loop.parse_ratings(ratings_raw)

    def _sync():
        session, engine = _tp._get_talkmaster_session()
        try:
            rev, reply = _rev(session, args)
            if reply:
                return reply
            today = date.today()
            d = loop.delivery_date(rev)
            if d is None:
                return _t("debrief_no_date", args)
            if today < d:
                return _t("debrief_too_soon", args, days=(d - today).days, date=d.isoformat())
            if not landed or not did_not:
                return _t("debrief_ask", args)
            focus = loop.focus_study_points(rev)
            kept = {n: v for n, v in ratings.items() if n in focus} if focus else ratings
            loop.record_debrief(session, rev, landed, did_not, kept)
            session.commit()
            lang = _lang_of(args)
            missing = [n for n in focus if n not in kept]
            lines = [_t("debrief_ok", args, name=rev.version_name)]
            if missing:
                lines.append(_t("debrief_rate_focus", args, points="; ".join(loop.focus_names([str(n) for n in missing], lang))))
            lines.append(_t("debrief_next", args))
            return "\n".join(lines)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"debrief_talk failed: {e}")
        return _t("error", args)


def mark_mastered(args: dict, **kwargs) -> str:
    """Mark a study point mastered: `mark mastered 8`. Only the speaker sets it, and only after
    the debrief of the talk it was a focus of.

    Args:
        point: The study point, 1 to 20.
    """
    from talkmaster import loop, standard

    try:
        point = int(str(args.get("point") or args.get("study_point") or "").strip().split(".")[0])
    except ValueError:
        return _t("mastered_ask", args)
    if standard.study_point(point) is None:
        return _t("mastered_ask", args)

    def _sync():
        session, engine = _tp._get_talkmaster_session()
        try:
            rev, reply = _rev(session, args)
            if reply:
                return reply
            if loop.debrief_of(session, rev.id) is None:
                return _t("mastered_needs_debrief", args)
            loop.mark_mastered(session, rev, point)
            session.commit()
            return _t("mastered_ok", args, point=standard.name(point, _lang_of(args)))
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"mark_mastered failed: {e}")
        return _t("error", args)


def my_progress(args: dict, **kwargs) -> str:
    """Where you stand, study point by study point: how often it was your focus, the last score
    on your text, your last self-rating, and the day you marked it mastered."""
    from talkmaster import loop

    def _sync():
        session, engine = _tp._get_talkmaster_session()
        try:
            rows = loop.progress(session, _lang_of(args))
            if not rows:
                return _t("progress_none", args)
            lines = [_t("progress_title", args)]
            for r in rows:
                bits = []
                if r["focused"]:
                    bits.append(_t("progress_focused", args, n=r["focused"]))
                if r["last_score"] is not None:
                    bits.append(_t("progress_score", args, score=r["last_score"]))
                if r["last_self"] is not None:
                    bits.append(_t("progress_self", args, score=r["last_self"]))
                if r["mastered_on"]:
                    bits.append(_t("progress_mastered", args, date=r["mastered_on"].isoformat()))
                lines.append(f"• {r['name']}: " + ", ".join(bits))
            return "\n".join(lines)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"my_progress failed: {e}")
        return _t("error", args)
