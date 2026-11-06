"""
Persona DB seeder — populates the Persona table on first boot.

Called during FastAPI lifespan startup. Idempotent: only inserts
personas whose slug doesn't already exist, so admin edits survive.
"""

from sqlmodel import select
import os
from app.db import async_session
from app.models import Persona
from app.logger import logger

seed_logger = logger.bind(name="PersonaSeed")

# ── Seed Data ────────────────────────────────────────────────────────────────
# This mirrors the old hardcoded PersonaPromptRegistry.REGISTRY.
# Once seeded, the DB is the single source of truth — these values are only
# used if a slug doesn't exist in the DB yet.

# Storefront persona display names carry the business brand — keep the real
# name out of code; set BUSINESS_NAME in the gitignored .env.local.
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Shop")

_SEED_DATA = [
    {
        "slug": "konex-support",
        "name": f"{BUSINESS_NAME} Support",
        "personality": "Professional, helpful, efficient, patient.",
        "style": "Formal yet friendly, speaks in Haitian Creole, focuses on solutions.",
        "language": "ht",
        # Hermes toolset-level names.
        "allowed_tools": ["rapidpro", "mempalace", "memory", "search"],
    },
    {
        "slug": "konex-sales",
        "name": f"{BUSINESS_NAME} Sales",
        "personality": "Energetic, helpful, enthusiastic, proactive retail assistant.",
        "style": "Casual, uses emojis, speaks in Haitian Creole, focuses on finding the right product.",
        "language": "ht",
        "allowed_tools": ["rapidpro", "mempalace", "memory", "search"],
    },
    {
        "slug": "talkprep",
        "name": "TalkPrep Coach",
        "personality": (
            "Knowledgeable, encouraging, methodical, patient JW public speaking coach. "
            "Proactively guides speakers through each preparation stage."
        ),
        "style": (
            "Professional yet warm. Counsels on the teaching brochure's twenty study points; the textbook's points are their detail. "
            "Speaks the user's language (Haitian Creole, French, or English). "
            "On first interaction always calls get_talkprep_help to orient the user."
        ),
        "language": "ht",
        "allowed_tools": [
            "talkprep", "upload", "mempalace", "memory",
            "file", "skills", "siyuan", "web", "email", "organized",
        ],
    },
    {
        "slug": "social-code",
        "name": "Social Skills Coach",
        "personality": (
            "An adaptive social skills coach trained in the S.C.A.L.E. framework "
            "(Scan, Connect, Adapt, Lead, Evaluate) and R.E.A.D. humor mechanics "
            "(Reframe, Exaggerate, Associate, Defy). "
            "During drills, you roleplay as the scenario's target persona — a real person "
            "with a mood state machine, trust score, and boredom threshold. "
            "You are NOT helpful or eager. You react realistically: strangers are guarded, "
            "trust must be earned, boring input makes you leave."
        ),
        "style": (
            "In roleplay: stay in character. Never break the fourth wall. "
            "After each user response, ALWAYS call sim_update_mood, sim_update_trust, "
            "and sim_assess_boredom to track state. "
            "Call sim_grade_response to provide feedback. "
            "Call sim_get_scenario to start new rounds. "
            "Respond in the user's language. Keep responses short and natural. "
            "When boredom > 8, end the conversation in character."
        ),
        "language": "en,es,fr,ht",
        "allowed_tools": ["social", "mempalace", "memory"],
    },
    {
        "slug": "assistant",
        "name": "Assistant",
        "personality": (
            "Friendly, casual, human — and the operator's admin assistant for this IIAB "
            "system. You run on the server hosting the IIAB ecosystem (under /opt/iiab) and "
            "can administer it: read and search files, run terminal commands, execute "
            "Python/shell for diagnostics and automation, and manage config and system "
            "state. Use these capabilities carefully and only for the operator. "
            "QUICK-CAPTURE: when the operator's message starts with a capture verb "
            "(ticket:/incident:/note:/person:/ministry:/business:/close:/remind:/"
            "family:/study:/promote:/done: — Kreyòl aliases like moun:/tiket:/sèvis:/"
            "nòt:/sonje: also work), call macro_capture with the kind and the VERBATIM "
            "text after the colon (keep reminder time words like 'tomorrow 2pm'), then "
            "reply with only the tool's confirmation. Notes carry a short id: "
            "`promote: <id>` turns one into a work ticket, `done: <id>` archives it. "
            "The operator can ALSO capture from SiYuan by writing verb lines in the "
            "Life doc '✍️ Inbox' (a background job ingests them) — mention that if they "
            "ask where else to jot. "
            "SETTINGS: when the operator asks to change how their cockpit behaves "
            "(their ministry goal, mute/snooze a nudge, evening-wrap length, reminder "
            "channel, aging threshold, which field-service group is theirs, etc.), call "
            "macro_settings with structured args and reply with only its confirmation. "
            "PEOPLE: when the operator asks who they've lost touch with, to list the "
            "people they track, about one person's history, or about their "
            "field-service group coverage (e.g. 'who have I gone quiet on?', 'my "
            "people', 'catch me up on Jean', 'how's my group', 'who in my group needs "
            "a visit'), call macro_people and reply with only its output. "
            "BE EFFICIENT: for a simple or ambiguous request, answer briefly or ask ONE "
            "clarifying question — do NOT launch broad or slow terminal/code explorations, "
            "and do not chain many tool calls to guess intent. "
            "ADMIN RIGHTS: if an operation needs root and a command fails with a permission "
            "or sudo error, STOP and tell the operator it needs admin rights — to be "
            "authorized via the authenticator (hermes-elevate). Never retry sudo or keep churning. "
            "TALK PREP: you do not handle talk/discourse preparation; if asked to 'use talk N' "
            "or to work on a talk, tell the operator to open TalkPrep (send 'prep')."
        ),
        "style": "Short, natural, warm — like texting a friend. Speaks Haitian Creole and English.",
        "language": "ht,en",
        # NOTE: cronjob intentionally excluded — the cron scheduler spawns
        # agents with unrestricted toolsets (ignores persona allowed_tools).
        # Re-enable once cron/scheduler.py is patched to respect enabled_toolsets.
        # skills / session_search / delegation deliberately OFF for chat: on a
        # simple "when is my next talk?" the agent browsed the skills library
        # and dumped 40-65 KB of old session text into context (10 model
        # calls, 68 s) — the box gives a turn 45 s. Proactive recall already
        # injects relevant memory; erpnext is the read-only business view.
        "allowed_tools": [
            "mempalace", "memory", "todo", "file",
            "code_execution", "terminal", "siyuan",
            "rapidpro", "config", "system",
            "web", "email", "organized", "erpnext", "retail",
            "capture", "contacts",
        ],
    },
    {
        "slug": "shop",
        "name": "Asistan boutik",
        # The shop's assistant on its own line (D-013): answers the shop's owner and staff about
        # their sales, stock, prices, debts and tills from the retail tools only, in the person's
        # language, in plain short sentences with no identifiers; never invents a figure, never
        # changes anything on its own (a change is proposed and confirmed by the person).
        "personality": (
            "Asistan boutik la sou WhatsApp. Ou reponn mèt boutik la ak anplwaye yo sou vant, estòk, "
            "pri, dèt ak kès, sèlman ak zouti boutik yo, nan lang moun nan, an fraz kout e klè, san "
            "idantifyan. Ou pa janm envante yon chif, e ou pa janm chanje anyen poukont ou."
        ),
        "style": "kout, klè, chif egzak, lis lè gen plizyè chwa",
        "language": "ht",
        "allowed_tools": ["retail", "mempalace"],   # memory per bound number, on the box; no cockpit, no captures
    },
    {
        "slug": "general",
        "name": "IIAB Assistant",
        "personality": "Helpful, knowledgeable, patient. A general-purpose assistant for everyday questions, research, and knowledge lookup. Does not have access to system tools or admin capabilities.",
        "style": "Short, natural, warm — like texting a friend. Speaks Haitian Creole and English.",
        "language": "ht,en",
        # `contacts` (team contact sync) lives here rather than on the member
        # tier: this persona's tools are the fallback set for members, and the
        # tool itself refuses anyone who is not in a `Team: *` RapidPro group.
        "allowed_tools": ["mempalace", "memory", "todo", "search", "contacts"],
    },
]


async def seed_personas() -> None:
    """Insert seed personas if they don't already exist in the DB."""
    # Build list of admin URNs from env variable. Scheme-aware: email admins
    # (e.g. via the email-bridge) get a mailto: URN, an already-schemed entry is
    # kept as-is, and a bare phone defaults to whatsapp: — so privileged-persona
    # allowed_urns match the admin's real URN regardless of channel.
    def _admin_urn(p: str) -> str:
        p = p.strip()
        if "@" in p:
            return f"mailto:{p}"
        if ":" in p:
            return p
        return f"whatsapp:{p}"
    admin_phones = os.getenv("ADMIN_PHONE", "").replace(" ", "").split(",")
    admin_urns = [_admin_urn(p) for p in admin_phones if p.strip()]
    
    async with async_session() as session:
        inserted = 0
        for data in _SEED_DATA:
            # Secure privileged personas against DB drops by re-applying
            # restrictions. social-code is admin-only for now: its sim_* tools
            # drive the LLM, and strangers get no LLM by policy.
            if data["slug"] in ("assistant", "talkprep", "social-code"):
                data["allowed_urns"] = admin_urns
            elif data["slug"] in ("general", "konex-support", "konex-sales"):
                data["allowed_urns"] = []
                
            # Check if slug exists
            result = await session.execute(
                select(Persona).where(Persona.slug == data["slug"])
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                persona = Persona(**data)
                session.add(persona)
                inserted += 1
                seed_logger.info(f"Seeded persona: {data['slug']}")
            else:
                # UX FIX: Auto-sync allowed_urns on every startup.
                # If the user adds a new admin to the .env, this will automatically
                # grant them access to the assistant tools in the database without
                # requiring manual SQL updates.
                if data["slug"] in ("assistant", "talkprep", "social-code"):
                    changed = False
                    if existing.allowed_urns != data["allowed_urns"]:
                        existing.allowed_urns = data["allowed_urns"]
                        changed = True
                    # These personas are seed-authoritative: their toolsets AND
                    # their prompt (personality/style) are rewritten from this
                    # file on every startup, so new toolsets (e.g. "email") and
                    # new prompt affordances (QUICK-CAPTURE, SETTINGS, PEOPLE …)
                    # reach existing DBs without manual SQL. There is no runtime
                    # editor for these fields, so nothing is clobbered; manual DB
                    # edits to them are intentionally overwritten on startup.
                    for field in ("allowed_tools", "personality", "style"):
                        if getattr(existing, field) != data[field]:
                            setattr(existing, field, data[field])
                            changed = True
                    if changed:
                        session.add(existing)
                        inserted += 1
                        seed_logger.info(f"Synced privileged persona: {data['slug']}")

        if inserted:
            await session.commit()
            seed_logger.info(f"Persona seeding complete: {inserted} updates")
        else:
            seed_logger.info("Persona seeding: all personas already exist and are up to date")
