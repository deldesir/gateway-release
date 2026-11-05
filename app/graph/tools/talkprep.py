"""
TalkPrep Hermes tools — full 6-stage workflow exposed to the gateway agent.

All blocking I/O is wrapped in asyncio.to_thread() so the async event loop
is never blocked even when talkmaster calls SQLite or runs LLM chains.
"""

import asyncio
import os
from typing import Optional

import httpx

from app.logger import setup_logger
from app.graph.tools import _talk_context as _tctx
from app.graph.tools.talkprep_text import t as _t, publication_name as _pub_name, language_name as _lang_name

logger = setup_logger().bind(name="tool.talkprep")

# Configurable jwlinker DB path (#8)
JWLINKER_DB_PATH: Optional[str] = os.getenv("JWLINKER_DB_PATH") or None


def _siyuan_doc_url(doc_id: str) -> str:
    """Build a clickable SiYuan URL for a document ID."""
    base = os.getenv("SIYUAN_PUBLIC_URL", "http://localhost:6806")
    return f"{base}/stage/build/desktop/?id={doc_id}"


# ── Helpers ─────────────────────────────────────────────────────────

_tm_db_ready = False


def _get_talkmaster_session():
    """Open a talkmaster DB session (sync — call from thread).

    Ensures the schema exists and the 53-point rubric is seeded — without
    the seed, every evaluation silently degrades to a 1-point rubric.
    """
    from talkmaster.config import get_settings
    from talkmaster.database import (
        Base, _seed_evaluation_points, get_engine, get_session,
    )
    global _tm_db_ready
    settings = get_settings()
    engine = get_engine(settings.db_path)
    if not _tm_db_ready:
        Base.metadata.create_all(engine)
        _seed_evaluation_points(get_session(engine))
        _tm_db_ready = True
    return get_session(engine), engine


# ── Talk identity: users speak in JW talk numbers, never internal IDs ─
#
# The talk the operator knows as "No 50" is stored with a name like
# "50. Fason pou nou pran bon desizyon" and an unrelated internal row id.
# Nobody should have to learn the row id — `use talk 50` must Just Work.

import re as _re


def _jw_number(name: str) -> Optional[str]:
    """The leading number in a talk name ('50. Fason...' → '50'), or None."""
    m = _re.match(r"\s*(?:no\.?\s*)?(\d+)\b", (name or "").strip(), _re.IGNORECASE)
    return m.group(1) if m else None


def talk_handle(talk) -> str:
    """The stable, human-facing handle for a talk — its JW number if it has
    one, else the internal id. This is what we print and what `use talk`
    accepts, so the two always agree."""
    return _jw_number(talk.name) or str(talk.id)


def _clean_title(name: str) -> str:
    """Talk name without the leading 'NN. ' number prefix, for display."""
    return _re.sub(r"^\s*(?:no\.?\s*)?\d+[.\)]?\s*", "", (name or "").strip())


def _disambiguation(talks, query: str) -> str:
    """List the candidates so each offers a selector that uniquely resolves.

    A JW number can repeat across languages (talk 102 in Creole and French),
    so the handle alone can't disambiguate — for those rows fall back to the
    unique internal id, which `_resolve_talk` accepts once the ambiguous JW
    number has been ruled out.
    """
    handles = [talk_handle(t) for t in talks]
    lines = [_t("disambiguation", None, query=query)]
    for t, h in zip(talks, handles):
        selector = h if handles.count(h) == 1 else str(t.id)
        lines.append(_t("disambiguation_line", None, selector=selector, title=_clean_title(t.name)))
    return "\n".join(lines)


def _resolve_talk(session, query):
    """Resolve whatever the user typed to a Talk row.

    Priority: revision code → JW talk number → internal id → name substring.
    Returns (talk, None) on a unique hit, or (None, message) when nothing
    matches or the match is ambiguous (message is user-facing, may be None).
    """
    from talkmaster.database import Revision, Talk

    q = str(query or "").strip()
    if not q:
        return None, None
    talks = session.query(Talk).all()

    # 1. exact revision code (e.g. 's34-50-w2026-07-06') → its talk
    rev = session.query(Revision).filter(Revision.version_name == q).first()
    if rev:
        t = next((x for x in talks if x.id == rev.talk_id), None)
        if t:
            return t, None

    # 2. JW talk number — the number the operator actually sees everywhere
    if q.isdigit():
        by_num = [t for t in talks if _jw_number(t.name) == q]
        if len(by_num) == 1:
            return by_num[0], None
        if len(by_num) > 1:
            return None, _disambiguation(by_num, q)
        # 3. only if no JW number matches, treat it as an internal id
        by_id = next((t for t in talks if t.id == int(q)), None)
        if by_id:
            return by_id, None

    # 4. name substring (case-insensitive)
    ql = q.lower()
    by_name = [t for t in talks if ql in (t.name or "").lower()]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, _disambiguation(by_name, q)

    return None, None


def _unique_version_name(session, talk, requested: str) -> str:
    """A globally-unique revision name that stays human-readable.

    Prefer exactly what the speaker asked for; if that name is already taken
    by another talk (the column is globally unique), prefix the talk number,
    then add a numeric suffix as a last resort.
    """
    from talkmaster.database import Revision

    requested = (requested or "v1").strip().replace(" ", "-") or "v1"
    handle = talk_handle(talk)

    def free(name: str) -> bool:
        return not session.query(Revision).filter_by(version_name=name).first()

    for cand in (requested, f"{handle}-{requested}"):
        if free(cand):
            return cand
    i = 2
    while True:
        cand = f"{handle}-{requested}-{i}"
        if free(cand):
            return cand
        i += 1


def _draft_progress(session, revision):
    """(drafted, total) sections for a revision, by the speaker's own drafts.

    'Drafted' is the same authoritative signal the morning nudge uses: a
    non-empty ✍️ draft in the section's SiYuan canvas. Returns drafted=None
    if SiYuan can't be reached, so callers degrade gracefully instead of
    crashing (the old code queried a non-existent `content_is_developed`
    column and blew up).
    """
    from talkmaster.database import StructureNode
    total = (
        session.query(StructureNode)
        .filter_by(revision_id=revision.id, node_type="section")
        .count()
    )
    try:
        from talkmaster.repositories import get_root_node_for_revision
        from talkmaster.siyuan.api import SiYuanAPI
        from talkmaster.siyuan.generation import _extract_speaker_draft

        root = get_root_node_for_revision(session, revision.id)
        if not root:
            return 0, total
        sections = [n for n in root.children if n.node_type == "section"]
        drafted = 0
        with SiYuanAPI() as siyuan:
            for node in sections:
                if not node.siyuan_doc_id:
                    continue
                canvas = siyuan.get_doc_content_markdown(node.siyuan_doc_id) or ""
                if _extract_speaker_draft(canvas):
                    drafted += 1
        return drafted, total
    except Exception as e:
        logger.warning(f"_draft_progress: SiYuan unavailable ({e})")
        return None, total


# ── Stage Gates (deterministic, zero AI) ─────────────────────────────

def _gate_talk_exists(session, talk_id: int) -> str | None:
    """Gate: talk must exist. Return error string or None."""
    from talkmaster.database import Talk
    if not session.query(Talk).filter_by(id=talk_id).first():
        return _t("gate_talk_missing", None)
    return None


def _gate_revision_exists(session, revision_name: str) -> str | None:
    """Gate: revision must exist. Return error string or None."""
    from talkmaster.database import Revision
    if not session.query(Revision).filter_by(version_name=revision_name).first():
        return _t("gate_revision_missing", None, name=revision_name)
    return None


def _gate_section_developed(session, revision_name: str) -> str | None:
    """Gate: the speaker must have drafted at least one section before we
    evaluate (evaluation scores the speaker's own drafts)."""
    from talkmaster.database import Revision
    rev = session.query(Revision).filter_by(version_name=revision_name).first()
    if not rev:
        return _gate_revision_exists(session, revision_name)
    drafted, _total = _draft_progress(session, rev)
    if drafted == 0:
        return _t("gate_no_draft", None, name=revision_name)
    return None


def _gate_evaluation_done(session, revision_name: str) -> str | None:
    """Gate: evaluation must have been run before rehearsal/export."""
    from talkmaster.database import Revision, EvaluationScore
    rev = session.query(Revision).filter_by(version_name=revision_name).first()
    if not rev:
        return _gate_revision_exists(session, revision_name)
    count = session.query(EvaluationScore).filter_by(revision_id=rev.id).count()
    if count == 0:
        return _t("gate_no_eval", None, name=revision_name)
    return None


def _gate_rehearsal_done(session, revision_name: str) -> str | None:
    """Gate: at least one rehearsal session must exist before export."""
    from talkmaster.database import Revision, RehearsalRecord
    rev = session.query(Revision).filter_by(version_name=revision_name).first()
    if not rev:
        return _gate_revision_exists(session, revision_name)
    count = session.query(RehearsalRecord).filter_by(revision_id=rev.id).count()
    if count == 0:
        return _t("gate_no_rehearsal", None)
    return None


# ── Stage 0: Status & Help ───────────────────────────────────────────

def get_talkprep_help(args: dict, **kwargs) -> str:
    """Return an onboarding guide for new TalkPrep users.

    Returns:
        A formatted guide explaining available commands and workflow stages.
    """
    return _t("help", args)


def talkmaster_status(args: dict, **kwargs) -> str:
    """Check the current talkmaster status: imported talks, revisions.

    Returns:
        A summary table of imported talks and their revisions.
    """
    def _sync():
        from talkmaster.database import Revision, StructureNode, Talk
        from app.graph.tools import _talk_context as ctx
        session, engine = _get_talkmaster_session()
        try:
            talks = session.query(Talk).all()
            if not talks:
                return _t("no_talks", args)
            active_id = ctx.get(args.get("user_id")).get("talk_id")
            lines = [_t("status_title", args)]
            for t in talks:
                handle = talk_handle(t)
                marker = _t("active_mark", args) if t.id == active_id else ""
                lines.append(_t("status_talk", args, handle=handle, title=_clean_title(t.name), active=marker))
                revs = session.query(Revision).filter_by(talk_id=t.id).all()
                if not revs:
                    lines.append(_t("status_no_revision", args))
                for r in revs:
                    drafted, total = _draft_progress(session, r)
                    thread = _t("thread_set", args) if r.golden_thread else _t("thread_missing", args)
                    if drafted is None:
                        prog = _t("prog_sections", args, total=total)
                    else:
                        prog = _t("prog_drafted", args, drafted=drafted, total=total)
                    lines.append(_t("status_rev", args, name=r.version_name, thread=thread, prog=prog))
            lines.append(_t("status_next", args, handle=talk_handle(talks[0])))
            return "\n".join(lines)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"talkmaster_status failed: {e}")
        return _t("error", args)


def select_active_talk(args: dict, **kwargs) -> str:
    # Accept the reference under any of the names the callers use: the rive
    # positional arg maps to `talk_id`, humans may also say `talk`/`query`.
    query = (
        args.get("talk_id") or args.get("talk")
        or args.get("query") or args.get("reference") or ""
    )
    user_id = args.get("user_id")

    """Select the talk to work on. Accepts the JW talk number you see
    everywhere ('50'), a revision code, or part of the talk's name \u2014 never
    an internal ID. The choice is remembered server-side for this person, so
    every later step (create revision, develop section\u2026) knows the talk.

    Args:
        talk_id: What the user typed \u2014 a JW number, revision code, or name.

    Returns:
        Confirmation with the talk name and its revisions.
    """
    if not str(query).strip():
        return _t("select_which", args)

    def _sync():
        from talkmaster.database import Revision
        from app.graph.tools import _talk_context as ctx
        session, engine = _get_talkmaster_session()
        try:
            talk, msg = _resolve_talk(session, query)
            if not talk:
                return msg or _t("select_unknown", args, query=query)
            ctx.set_talk(user_id, talk.id)
            revs = session.query(Revision).filter_by(talk_id=talk.id).all()
            handle = talk_handle(talk)
            if revs:
                rev_lines = "\n".join(
                    _t("select_rev_line", args, name=r.version_name,
                       thread=_t("select_thread_mark", args) if r.golden_thread else "")
                    for r in revs
                )
                # A single revision is the obvious one to work on \u2014 adopt it.
                if len(revs) == 1:
                    ctx.set_revision(user_id, revs[0].version_name)
                rev_block = "\n" + _t("select_revisions", args) + "\n" + rev_lines
                nxt = _t("select_next_with_revs", args)
            else:
                rev_block = "\n" + _t("select_no_revisions", args)
                nxt = _t("select_next_no_revs", args)
            return _t("select_ok", args, handle=handle, title=_clean_title(talk.name)) + rev_block + "\n\n" + nxt
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"select_active_talk failed: {e}")
        return _t("error", args)


# ── Stage 1: Import ──────────────────────────────────────────────────

def list_publications(args: dict, **kwargs) -> str:
    """List all available JW publications in the jwlinker database.

    Returns:
        A formatted list of publications with codes and topic counts.
    """
    def _sync():
        from talkmaster.bridge import list_jwlinker_publications
        pubs = list_jwlinker_publications(JWLINKER_DB_PATH)
        if not pubs:
            return _t("pubs_none", args)
        # One line per publication, its languages named, not numbered.
        by_code: dict = {}
        for p in pubs:
            entry = by_code.setdefault(p["code"], {"langs": [], "topics": 0})
            entry["langs"].append(_lang_name(p["language"], args))
            entry["topics"] = max(entry["topics"], p["topic_count"])
        lines = [_t("pub_line", args, name=_pub_name(code, args), langs=", ".join(e["langs"]), topics=e["topics"])
                 for code, e in sorted(by_code.items())]
        return _t("pubs_title", args) + "\n" + "\n".join(lines)

    try:
        return _sync()
    except Exception as e:
        logger.error(f"list_publications failed: {e}")
        return _t("error", args)


def list_topics(args: dict, **kwargs) -> str:
    pub_code = args.get("pub_code")
    active_pub = args.get("active_pub")

    """List all topics (talk outlines) for a given publication code.

    Args:
        pub_code: Publication code, e.g. 's-34', 'lmd', 'scl'.
        active_pub: Auto-injected from context (not user-facing).

    Returns:
        Formatted list of available topics with categories.
    """
    # Fall back to the user's remembered publication if none was given.
    effective_pub = pub_code.strip() if pub_code and pub_code.strip() else (
        _tctx.active_pub(args) or "")
    if not effective_pub:
        return _t("topics_need_pub", args)

    def _sync():
        from talkmaster.bridge import list_jwlinker_topics
        topics = list_jwlinker_topics(effective_pub, db_path=JWLINKER_DB_PATH)
        if not topics:
            return _t("topics_none", args, pub=_pub_name(effective_pub, args))
        _tctx.set_pub(args.get("user_id"), effective_pub)
        lines = [_t("topic_line", args, name=t["name"], category=t["category"]) for t in topics[:30]]
        suffix = _t("topics_more", args, n=len(topics)) if len(topics) > 30 else ""
        return (_t("topics_title", args, pub=_pub_name(effective_pub, args)) + "\n" + "\n".join(lines)
                + suffix + _t("topics_next", args))

    try:
        return _sync()
    except Exception as e:
        logger.error(f"list_topics failed: {e}")
        return _t("error", args)


def import_talk(args: dict, **kwargs) -> str:
    topic_query = args.get("topic_query")
    pub_code_arg = args.get("pub_code")
    active_pub = args.get("active_pub")

    """Import a talk outline from a JW Library publication into talkmaster.

    Args:
        pub_code: Publication code (e.g. 's-34').
        topic_query: Topic name or number to search for.
            Can be the full topic name, a partial match, or "No 26"-style.
            The pub_code is auto-inferred from the last listed/uploaded publication.
        active_pub: Auto-injected from context (not user-facing).

    Returns:
        Confirmation with imported talk details and its new ID.
    """
    def _sync():
        from talkmaster.bridge import import_from_jwlinker, save_imported_talk

        pub_code = pub_code_arg or _tctx.active_pub(args) or ""
        if not pub_code:
            return _t("import_no_pub", args)

        # Use the entire topic_query as the topic name search
        theme = topic_query  # default theme = topic name
        talk = import_from_jwlinker(
            pub_code=pub_code,
            topic_name=topic_query,
            talk_theme=theme,
            language="en",
            jwlinker_db_path=JWLINKER_DB_PATH,
        )
        talk_id = save_imported_talk(talk)
        sections = len(talk.outline)
        points = sum(len(s.discussion_points) for s in talk.outline)
        # Auto-select the freshly imported talk so the next step just works.
        _tctx.set_talk(args.get("user_id"), talk_id)
        _tctx.set_pub(args.get("user_id"), pub_code)
        name = talk.talk_metadata.name
        handle = _jw_number(name) or str(talk_id)
        return _t("import_ok", args, handle=handle, title=_clean_title(name), sections=sections, points=points)

    try:
        return _sync()
    except Exception as e:
        logger.error(f"import_talk failed: {e}")
        return _t("error", args)


# ── Stage 2: Revision ────────────────────────────────────────────────

def create_revision(args: dict, **kwargs) -> str:
    requested_name = (args.get("version_name") or "").strip() or "v1"
    audience_description = args.get("audience_description", "General congregation audience")
    language = (args.get("language") or "ht").strip().lower()[:2]
    user_id = args.get("user_id")
    """Create a new revision of a talk with an audience persona and golden thread.

    Args:
        version_name: Unique name for this revision (e.g., 'v1', 'young-adults').
        audience_description: Description of the target audience.

    Returns:
        Confirmation with revision details and next steps.
    """
    from app.graph.tools import _talk_context as ctx
    # Which talk? Server-side memory (set by `use talk`), header override honored.
    talk_id = ctx.active_talk_id(args)
    if talk_id is None:
        return _t("rev_which_talk", args)

    def _sync():
        from talkmaster.database import (
            Talk, Revision, AudiencePersona, create_structure_nodes_from_outline,
        )
        from talkmaster.outline_parser import parse_talk_outline_from_string
        from talkmaster.siyuan.structure import create_siyuan_structure

        session, engine = _get_talkmaster_session()
        try:
            gate = _gate_talk_exists(session, talk_id)
            if gate:
                return gate

            talk = session.query(Talk).filter_by(id=talk_id).first()
            # version_name is globally unique in the schema, but a speaker
            # naturally types "v1" for every talk. Keep the friendly name when
            # it's free; otherwise namespace it with the talk number so
            # `create revision v1` never collides across talks.
            same_talk = session.query(Revision).filter_by(
                talk_id=talk_id, version_name=requested_name
            ).first()
            if same_talk:
                return _t("rev_exists", args, name=requested_name)
            version_name = _unique_version_name(session, talk, requested_name)
            persona_name = version_name
            persona = session.query(AudiencePersona).filter_by(name=persona_name).first()
            if not persona:
                persona = AudiencePersona(
                    name=persona_name,
                    description=audience_description,
                )
                session.add(persona)
                session.flush()

            revision = Revision(
                talk_id=talk_id,
                version_name=version_name,
                persona_id=persona.id,
                language=language,
                notebook_id=os.getenv("SIYUAN_NOTEBOOK_ID", ""),
            )
            session.add(revision)
            session.commit()

            # Build the full coaching workspace: structure nodes + SiYuan
            # canvases (with the '\u270d\ufe0f My Draft' scaffold). Without this the
            # draft \u2192 challenge \u2192 assemble workflow has nowhere to live.
            # SiYuan is a local service that can be briefly down; if it is, we
            # keep the revision (already committed) and tell the user the
            # canvases are pending rather than erroring out and orphaning it.
            talk_model = parse_talk_outline_from_string(talk.outline_json)
            canvas_note = ""
            if talk_model:
                try:
                    create_structure_nodes_from_outline(session, revision, talk_model)
                    root_doc_id = create_siyuan_structure(revision, talk_model, session)
                    session.commit()
                    if root_doc_id:
                        canvas_note = _t("rev_canvases_ready", args, url=_siyuan_doc_url(root_doc_id))
                except Exception as canvas_err:
                    session.rollback()
                    logger.warning(
                        f"create_revision: canvases deferred for "
                        f"'{version_name}': {canvas_err}"
                    )
                    canvas_note = _t("rev_canvases_deferred", args)

            ctx.set_revision(user_id, version_name)
            return _t("rev_created", args, name=version_name, handle=talk_handle(talk),
                      title=_clean_title(talk.name), audience=audience_description, note=canvas_note)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"create_revision failed: {e}")
        return _t("error", args)


# ── Stage 3: Speaker draft + coaching ────────────────────────────────

def challenge_section(args: dict, **kwargs) -> str:
    section_title = args.get("section_title")
    active_revision = args.get("active_revision")

    """Socratic coaching on the SPEAKER'S OWN draft (the primary workflow).

    Never writes prose: cites weak rubric points in the draft, asks
    questions the speaker must answer in writing, and issues out-loud
    speaking challenges. Requires the speaker's draft in the canvas.

    Args:
        section_title: Title of the section to challenge.
        active_revision: Auto-injected from context (not user-facing).

    Returns:
        The challenge report (also appended to the section canvas).
    """
    revision_name = _tctx.active_revision(args) or ""
    if not revision_name:
        return _t("no_active_revision", args)

    def _sync():
        from talkmaster.siyuan import generation
        session, engine = _get_talkmaster_session()
        try:
            gate = _gate_revision_exists(session, revision_name)
            if gate:
                return gate
            report = generation.challenge_section(
                version_name=revision_name,
                db=session,
                section_title=section_title,
            )
            return _t("challenge", args, section=section_title, report=report)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        from talkmaster.siyuan.generation import DraftRequiredError, SectionNotFoundError
        if isinstance(e, SectionNotFoundError):
            return f"🤔 {e}"
        if isinstance(e, DraftRequiredError):
            return f"✍️ {e}"
        logger.error(f"challenge_section failed: {e}")
        return _t("error", args)


def develop_section(args: dict, **kwargs) -> str:
    section_title = args.get("section_title")
    active_revision = args.get("active_revision")

    """Generate the AI *reference* manuscript for a section (compare-only).

    Gated: unlocks only after the speaker's own draft exists in the canvas.
    The reference is studied AGAINST the speaker's draft, never delivered
    as-is. For coaching, use challenge_section.

    Args:
        section_title: Title of the section (partial match OK).
        active_revision: Auto-injected from context (not user-facing).

    Returns:
        Confirmation or status of the generated reference.
    """
    revision_name = _tctx.active_revision(args) or ""
    if not revision_name:
        return _t("no_active_revision", args)

    def _sync():
        from talkmaster.siyuan import generation
        session, engine = _get_talkmaster_session()
        try:
            gate = _gate_revision_exists(session, revision_name)
            if gate:
                return gate

            result = generation.develop_section(
                version_name=revision_name,
                db=session,
                section_title=section_title,
            )
            if result:
                preview = result[:400] + "\u2026" if len(result) > 400 else result
                return _t("develop_ok", args, section=section_title, preview=preview)
            return _t("develop_failed", args, section=section_title)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        from talkmaster.siyuan.generation import DraftRequiredError, SectionNotFoundError
        if isinstance(e, SectionNotFoundError):
            return f"\ud83e\udd14 {e}"
        if isinstance(e, DraftRequiredError):
            return f"\u270d\ufe0f {e}"
        logger.error(f"develop_section failed: {e}")
        return _t("error", args)


# ── Stage 4: Evaluation ──────────────────────────────────────────────

def set_golden_thread(args: dict, **kwargs) -> str:
    thread = (args.get("thread") or "").strip()
    active_revision = args.get("active_revision")
    revision_name = args.get("revision_name")

    """Set the golden thread — the speaker's central message, in their words.

    The thread anchors every coaching stage (the challenge audits drift
    from it; the memory deck memorizes it). It must come from the SPEAKER
    — never propose one; ask them to state it.

    Args:
        thread: The speaker's central message (one sentence, their words).
        active_revision: Auto-injected from context.
        revision_name: Explicit override.
    """
    rev_name = _tctx.active_revision(args) or ""
    if not rev_name:
        return _t("no_active_revision", args)
    if not thread:
        return _t("thread_ask", args)

    def _sync():
        from talkmaster.database import Revision
        session, engine = _get_talkmaster_session()
        try:
            rev = session.query(Revision).filter_by(version_name=rev_name).first()
            if not rev:
                return _t("rev_not_found", args, name=rev_name)
            rev.golden_thread = thread
            session.commit()
            return _t("thread_done", args, name=rev_name, thread=thread)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"set_golden_thread failed: {e}")
        return _t("error", args)


def evaluate_talk(args: dict, **kwargs) -> str:
    active_revision = args.get("active_revision")
    revision_name = args.get("revision_name")
    self_score = args.get("self_score")

    """Evaluate the SPEAKER'S OWN manuscript against the text-assessable rubric.

    Requires self_score (1-10): the speaker's honest overall rating BEFORE
    seeing the AI's — ask for it first if not provided. Harvests the
    speaker's drafts from the section canvases, scores the 33
    text-assessable rubric points with quoted evidence, persists scores,
    and reports the self-vs-AI calibration.

    Args:
        active_revision: Auto-injected from context.
        revision_name: Explicit override (from AI/direct call).
        self_score: The speaker's own overall score, 1-10 (REQUIRED —
            ask the speaker before calling).
    """
    rev_name = _tctx.active_revision(args) or ""
    if not rev_name:
        return _t("no_active_revision", args)
    try:
        self_score_val = max(1.0, min(10.0, float(self_score)))
    except (TypeError, ValueError):
        return _t("eval_ask_self", args)

    def _sync():
        import json as _json

        from talkmaster.database import (
            EvaluationPoint, EvaluationScore, Revision, WorkflowRun,
        )
        from talkmaster.siyuan import generation
        from talkmaster.workflows.graphs import evaluation_workflow

        session, engine = _get_talkmaster_session()
        try:
            rev = session.query(Revision).filter_by(version_name=rev_name).first()
            if not rev:
                return _t("rev_not_found", args, name=rev_name)

            manuscript, missing = generation.collect_speaker_manuscript(rev_name, session)
            if not manuscript:
                return _t("eval_no_drafts", args)

            state = {
                "manuscript_text": manuscript,
                "ai_draft": "",
                "language": rev.language or "en",
                "db_session": session,
            }
            result = evaluation_workflow.invoke(state)
            evaluation = result.get("evaluation_result", {})
            scores = evaluation.get("scores", [])

            for sc in scores:
                point = session.query(EvaluationPoint).filter_by(
                    number=sc.get("point_number")).first()
                if point:
                    fb = sc.get("feedback", "")
                    if sc.get("evidence"):
                        fb = f"\u201c{sc['evidence']}\u201d — {fb}"
                    session.add(EvaluationScore(
                        revision_id=rev.id,
                        evaluation_point_id=point.id,
                        score=sc.get("score", 0),
                        feedback=fb,
                        overall_score=evaluation.get("overall_score", 0),
                    ))
            session.add(WorkflowRun(
                revision_id=rev.id,
                workflow_name="self_assessment",
                status="completed",
                context_json=_json.dumps({
                    "self_score": self_score_val,
                    "ai_score": evaluation.get("overall_score"),
                }),
            ))
            session.commit()

            ai_overall = evaluation.get("overall_score")
            lines = [_t("eval_title", args, name=rev_name)]
            if isinstance(ai_overall, (int, float)):
                delta = self_score_val - float(ai_overall)
                if abs(delta) < 1.0:
                    verdict = _t("verdict_ok", args)
                elif delta > 0:
                    verdict = _t("verdict_high", args)
                else:
                    verdict = _t("verdict_low", args)
                lines.append(_t("eval_calibration", args, own=self_score_val, ai=ai_overall, delta=delta, verdict=verdict))
            if missing:
                lines.append(_t("eval_missing", args, missing=", ".join(missing[:3])))
            for s in (evaluation.get("strengths") or [])[:3]:
                lines.append(f"\u2705 {s}")
            for imp in (evaluation.get("improvements") or [])[:3]:
                lines.append(f"\U0001f536 {imp}")
            lines.append(_t("eval_next", args))
            return "\n".join(lines)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"evaluate_talk failed: {e}")
        return _t("error", args)


def get_evaluation_scores(args: dict, **kwargs) -> str:
    active_revision = args.get("active_revision")
    revision_name = args.get("revision_name")

    """Get S-38 rubric evaluation scores for a talk revision.

    Args:
        active_revision: Auto-injected from context.
        revision_name: Explicit override.

    Returns:
        Scores broken down by S-38 category with coaching tips.
    """
    rev_name = _tctx.active_revision(args) or ""
    if not rev_name:
        return _t("no_active_revision", args)
    def _sync():
        from talkmaster.database import Revision, EvaluationScore, EvaluationPoint
        session, engine = _get_talkmaster_session()
        try:
            rev = session.query(Revision).filter_by(version_name=rev_name).first()
            if not rev:
                return _t("rev_not_found", args, name=rev_name)

            scores = (
                session.query(EvaluationScore)
                .join(EvaluationPoint)
                .filter(EvaluationScore.revision_id == rev.id)
                .all()
            )
            if not scores:
                return _t("scores_none", args, name=rev_name)

            # Group by category
            by_cat: dict = {}
            for s in scores:
                cat = s.evaluation_point.category
                by_cat.setdefault(cat, []).append(s.score)

            # Rubric scores are 1-5 (see talkmaster EvaluationScore / evaluate.j2)
            lines = [_t("scores_title", args, name=rev_name)]
            total, count = 0, 0
            for cat, cat_scores in sorted(by_cat.items()):
                avg = sum(cat_scores) / len(cat_scores)
                filled = min(5, max(0, round(avg)))
                bar = "█" * filled + "░" * (5 - filled)
                lines.append(f"• {cat}: {bar} {avg:.1f}/5")
                total += sum(cat_scores)
                count += len(cat_scores)

            overall = total / count if count else 0
            lines.append(_t("scores_overall", args, overall=overall))
            if overall < 3.0:
                lines.append(_t("scores_tip_low", args))
            elif overall < 4.0:
                lines.append(_t("scores_tip_mid", args))
            else:
                lines.append(_t("scores_tip_high", args))

            return "\n".join(lines)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"get_evaluation_scores failed: {e}")
        return _t("error", args)


# ── Stage 5: Rehearsal ───────────────────────────────────────────────

def rehearsal_cue(args: dict, **kwargs) -> str:
    active_revision = args.get("active_revision")
    revision_name = args.get("revision_name")

    """Generate AI delivery coaching cues for rehearsal of a talk revision.

    Args:
        active_revision: Auto-injected from context.
        revision_name: Explicit override.

    Returns:
        Personalized delivery cues: pacing, pauses, emphasis, eye contact.
    """
    rev_name = _tctx.active_revision(args) or ""
    if not rev_name:
        return _t("no_active_revision", args)
    def _sync():
        from talkmaster.database import Revision, RehearsalRecord
        from talkmaster.llm import completion
        import datetime

        session, engine = _get_talkmaster_session()
        try:
            # ── Stage gate: evaluation must have been done ────────────────
            gate = _gate_evaluation_done(session, rev_name)
            if gate:
                return gate

            rev = session.query(Revision).filter_by(version_name=rev_name).first()
            if not rev:
                return _t("rev_not_found", args, name=rev_name)

            # Pull rehearsal count for progression
            rehearsal_count = (
                session.query(RehearsalRecord)
                .filter_by(revision_id=rev.id)
                .count()
            )

            prompt = (
                f"You are a JW public speaking coach. The speaker is doing rehearsal "
                f"#{rehearsal_count + 1} of their talk revision '{rev_name}'.\n\n"
                f"Give 5 short, actionable delivery coaching tips covering:\n"
                f"1. Opening — how to establish eye contact immediately\n"
                f"2. Pacing — when to slow down for impact\n"
                f"3. Pausing — where to pause for effect (cite a specific point if possible)\n"
                f"4. Vocal modulation — how to vary tone\n"
                f"5. Closing — how to make the conclusion memorable\n\n"
                f"Be specific, warm, and concise. Each tip max 2 sentences."
            )

            # talkmaster.llm.completion — native Gemini, returns the text.
            cues = completion(prompt=prompt)

            # Log rehearsal record (created_at defaults to now; durations 0 —
            # this records the cue generation, not a timed run)
            record = RehearsalRecord(
                revision_id=rev.id,
                section_title="(coaching cues)",
                target_duration_seconds=0,
                actual_duration_seconds=0,
                notes=f"AI cues generated (rehearsal #{rehearsal_count + 1})",
            )
            session.add(record)
            session.commit()

            return _t("rehearsal_title", args, n=rehearsal_count + 1, name=rev_name, cues=cues)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"rehearsal_cue failed: {e}")
        return _t("error", args)


# ── Stage 6: Export ──────────────────────────────────────────────────

def export_talk_summary(args: dict, **kwargs) -> str:
    active_revision = args.get("active_revision")
    revision_name = args.get("revision_name")

    """Assemble and export the final talk manuscript summary.

    Args:
        active_revision: Auto-injected from context.
        revision_name: Explicit override.

    Returns:
        A condensed version of the full manuscript for review.
    """
    rev_name = _tctx.active_revision(args) or ""
    if not rev_name:
        return _t("no_active_revision", args)
    def _sync():
        from talkmaster.siyuan import generation
        session, engine = _get_talkmaster_session()
        try:
            # ── Stage gate: must have rehearsed at least once ─────────────
            gate = _gate_rehearsal_done(session, rev_name)
            if gate:
                return gate

            result = generation.assemble_manuscript(
                version_name=rev_name,
                db=session,
                source="ai",
            )
            if result:
                siyuan_url = _siyuan_doc_url(result)
                return _t("export_ok", args, name=rev_name, url=siyuan_url)
            return _t("export_failed", args, name=rev_name)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"export_talk_summary failed: {e}")
        return _t("error", args)


# ── Cost Reporting ───────────────────────────────────────────────────

def cost_report(args: dict, **kwargs) -> str:
    """Show LLM token usage and estimated cost for the current talkmaster session.

    Returns:
        Token counts and estimated USD cost for this preparation session.
    """
    def _sync():
        from talkmaster.llm import get_session_usage
        usage = get_session_usage()
        return _t("cost", args, prompt=usage.prompt_tokens, completion=usage.completion_tokens,
                  total=usage.total_tokens, usd=usage.estimated_cost_usd)

    try:
        return _sync()
    except Exception as e:
        logger.error(f"cost_report failed: {e}")
        return _t("error", args)


# ── JWLinker Integration ─────────────────────────────────────────────

def _get_jwlinker_cards(pub_code: str, topic_name: Optional[str] = None,
                        language: str = "Haitian", locale: str = "CR"):
    """Fetch parsed cards from jwlinker DB for a given publication.

    Returns a list of AnkiCard objects. Reuses jwlinker's existing
    get_cards_for_generate() pipeline with a synthetic args namespace.
    """
    from types import SimpleNamespace
    from jwlinker.commands.db import get_cards_for_generate

    args = SimpleNamespace(
        pub=pub_code,
        lang_id=None,
        language=language,
        locale=locale,
        format=None,
        category=None,
        topic=topic_name,
        verbose=False,
        stories_as_cards=False,
    )
    return get_cards_for_generate(args)


def generate_anki_deck(args: dict, **kwargs) -> str:
    pub_code = args.get("pub_code")
    topic_name = args.get("topic_name")

    """Generate an Anki flashcard deck (.apkg) from a JW publication in the database.

    The deck is saved and a download URL is returned so the user can
    fetch the .apkg file directly. If no topic is specified, all topics
    for the publication are included.

    Args:
        pub_code: Publication code (e.g., 's34', 'lmd', 'scl').
        topic_name: Optional topic name filter (partial match OK).

    Returns:
        Download URL for the generated .apkg file, or an error message.
    """
    def _sync():
        from jwlinker.exporters.anki import AnkiExporter
        from jwlinker.core.linker import Linker

        cards = _get_jwlinker_cards(pub_code, topic_name)
        if not cards:
            return _t("deck_none", args, what=_pub_name(pub_code, args) + (f" · {topic_name}" if topic_name else ""))
        linker = Linker(language="Haitian", locale="CR")
        deck_name = f"JW Study: {pub_code}"
        if topic_name:
            deck_name += f" — {topic_name}"
        exporter = AnkiExporter(root_deck_name=deck_name)
        exporter.add_cards(cards, linker, {})

        filename = f"jwlinker_{pub_code}{'_' + topic_name.replace(' ', '_') if topic_name else ''}.apkg"
        output_path = f"/tmp/{filename}"
        exporter.export(output_path)

        # A signed link: the route serves nothing without it (app/api/downloads.py)
        from app.api.downloads import download_url as signed_download_url

        download_url = signed_download_url(filename)

        return _t("deck_ok", args, n=len(cards), name=deck_name, url=download_url)

    try:
        return _sync()
    except Exception as e:
        logger.error(f"generate_anki_deck failed: {e}")
        return _t("error", args)


def push_to_siyuan(args: dict, **kwargs) -> str:
    pub_code = args.get("pub_code")
    topic_name = args.get("topic_name")

    """Push JW publication content to SiYuan as a structured document tree.

    Creates a two-level tree (sections → lessons) with scripture links and
    spaced-repetition flashcards. Requires SIYUAN_NOTEBOOK_ID env var.

    Args:
        pub_code: Publication code (e.g., 's34', 'lmd', 'scl').
        topic_name: Optional topic name filter (partial match OK).

    Returns:
        SiYuan root document ID, or an error message.
    """
    def _sync():
        import re
        from jwlinker.exporters.siyuan import SiYuanExporter

        notebook_id = os.getenv("SIYUAN_NOTEBOOK_ID")
        if not notebook_id:
            return _t("siyuan_not_configured", args)

        cards = _get_jwlinker_cards(pub_code, topic_name)
        if not cards:
            return _t("deck_none", args, what=_pub_name(pub_code, args) + (f" · {topic_name}" if topic_name else ""))
        # Format pub code for display (s34 → S-34)
        m = re.match(r'^([a-zA-Z]+)(\d+)$', pub_code)
        display_code = f"{m.group(1).upper()}-{m.group(2)}" if m else pub_code.upper()
        root_name = f"{display_code}_CR"

        exporter = SiYuanExporter(
            notebook_id=notebook_id,
            root_name=root_name,
            language="Haitian",
            locale="CR",
        )
        exporter.add_cards(cards)
        root_id = exporter.export(replace=False)

        siyuan_url = _siyuan_doc_url(root_id)
        return _t("siyuan_ok", args, n=len(cards), url=siyuan_url)

    try:
        return _sync()
    except Exception as e:
        logger.error(f"push_to_siyuan failed: {e}")
        return _t("error", args)


# ── Stage 5b: Voice-note rehearsal analysis ──────────────────────────

def rehearse_audio(args: dict, **kwargs) -> str:
    media_url = (args.get("media_url") or "").strip()
    section_title = (args.get("section_title") or "").strip()
    active_revision = args.get("active_revision")

    """Analyze a WhatsApp voice-note rehearsal: timing, pace, pausing.

    The falsifiable half of rehearsal: measured duration against the
    section target, plus a pause profile from the audio itself (no
    self-report). Persists a RehearsalRecord for progress tracking.

    Args:
        media_url: URL of the voice note (from the [Attachment: audio/...] line).
        section_title: Which section was rehearsed (optional; whole talk if omitted).
        active_revision: Auto-injected from context.
    """
    import re as _re
    import subprocess
    import tempfile

    revision_name = _tctx.active_revision(args) or ""
    if not media_url:
        return _t("audio_ask", args)
    if not revision_name:
        return _t("no_active_revision", args)

    def _sync():
        from talkmaster.database import RehearsalRecord, Revision
        from talkmaster.outline_parser import parse_talk_outline_from_string

        session, engine = _get_talkmaster_session()
        try:
            rev = session.query(Revision).filter_by(version_name=revision_name).first()
            if not rev:
                return _t("rev_not_found", args, name=revision_name)

            # Target duration: named section, else the whole talk
            talk_model = parse_talk_outline_from_string(rev.talk.outline_json)
            target_min, target_label = 0, _t("whole_talk", args)
            if talk_model:
                if section_title:
                    sec = next(
                        (s for s in talk_model.outline
                         if section_title.lower() in s.title.lower()), None)
                    if sec:
                        target_min, target_label = sec.duration_minutes, sec.title
                if not target_min:
                    target_min = sum(s.duration_minutes for s in talk_model.outline)

            # Download + probe
            with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
                with httpx.Client(timeout=60, follow_redirects=True) as client:
                    r = client.get(media_url)
                    r.raise_for_status()
                    tmp.write(r.content)
                audio_path = tmp.name

            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", audio_path],
                    capture_output=True, text=True, timeout=60,
                )
                duration = float(probe.stdout.strip())

                silence = subprocess.run(
                    ["ffmpeg", "-i", audio_path, "-af",
                     "silencedetect=noise=-30dB:d=0.6", "-f", "null", "-"],
                    capture_output=True, text=True, timeout=300,
                )
                sil_durs = [float(x) for x in _re.findall(
                    r"silence_duration:\s*([\d.]+)", silence.stderr)]
            finally:
                os.unlink(audio_path)

            pauses = len(sil_durs)
            silence_total = sum(sil_durs)
            longest = max(sil_durs) if sil_durs else 0.0
            speech_ratio = max(0.0, (duration - silence_total) / duration) if duration else 0

            target_s = target_min * 60
            elapsed = int(duration)
            diff_pct = abs(elapsed - target_s) / target_s * 100 if target_s else 0
            timing_score = (10 if diff_pct <= 5 else 8 if diff_pct <= 10
                            else 6 if diff_pct <= 20 else 4 if diff_pct <= 30 else 2)

            session.add(RehearsalRecord(
                revision_id=rev.id,
                section_title=target_label,
                target_duration_seconds=target_s,
                actual_duration_seconds=elapsed,
                timing_score=timing_score,
                notes=(f"voice note: {pauses} pause(s) ≥0.6s, "
                       f"{silence_total:.0f}s silence, longest {longest:.1f}s, "
                       f"speech ratio {speech_ratio:.0%}"),
            ))
            session.commit()

            mins, secs = divmod(elapsed, 60)
            lines = [
                _t("audio_title", args, label=target_label),
                _t("audio_timing", args, m=mins, s=secs, target=target_min, stars="⭐" * timing_score, score=timing_score),
                _t("audio_pauses", args, n=pauses, longest=longest),
            ]
            if pauses == 0 and duration > 60:
                lines.append(_t("audio_no_pauses", args))
            elif longest > 6:
                lines.append(_t("audio_long_gap", args, gap=longest))
            if speech_ratio > 0.97 and duration > 60:
                lines.append(_t("audio_continuous", args))
            lines.append(_t("audio_footer", args))
            return "\n".join(lines)
        finally:
            session.close()
            engine.dispose()

    try:
        return _sync()
    except Exception as e:
        logger.error(f"rehearse_audio failed: {e}")
        return _t("error", args)
