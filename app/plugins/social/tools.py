"""Social-Code Plugin — Native Hermes Simulation Tools.

Replaces the HTTP proxy stub with 7 tools that Hermes calls during
social skills training. State is persisted via RiveBot user variables,
ensuring per-user isolation (verified: RiveScript scopes vars by user_id).

Architecture:
  - When AI is available: Hermes calls these tools during its agent loop
  - When AI is unavailable: RiveBot triggers offline.py directly (golden set mode)
"""

import json
import logging
import os
from typing import Any, Dict

import httpx

from app.plugins import register_tool
from app.plugins.social.offline import (
    analyze_response_offline,
    compute_trust_delta,
    detect_sentiment,
    generate_offline_response,
)
from app.plugins.social.scenarios import (
    format_scenario_whatsapp,
    get_scenario,
    load_scenarios,
)
from app.plugins.social.mastery import (
    record_drill,
    get_session_stats,
    FSRS_LABELS,
)

logger = logging.getLogger(__name__)

RIVEBOT_URL = os.getenv("RIVEBOT_URL", "http://127.0.0.1:8087")


# ════════════════════════════════════════════════════════════════════════════
#  Lightweight Hermes Chat — structured prompts, no tool loop
# ════════════════════════════════════════════════════════════════════════════

def _hermes_chat(prompt: str, urn: str = "system:social-code") -> str | None:
    """Send a one-shot prompt to Hermes (no tools, no history).

    Uses AIAgent.chat() in minimal mode — same LLM config as the full
    agent but without the tool loop, history, or session overhead.
    Returns the text response, or None on failure.
    """
    try:
        from run_agent import AIAgent

        from app.hermes.provider import agent_provider_kwargs, model_for

        _llm_model = model_for("social-code") or "gemini-2.0-flash"

        agent_kwargs = dict(
            model=_llm_model,
            max_iterations=1,
            enabled_toolsets=[],       # No tools — pure LLM prompt
            ephemeral_system_prompt=prompt,
            session_id=f"social-onetime-{urn}",
            user_id=urn,
            platform="whatsapp",
            quiet_mode=True,
            skip_context_files=True,   # No SOUL.md — just the prompt
            skip_memory=True,          # No memory overhead
            verbose_logging=False,
        )

        # Same provider, key, fallback chain and effort as the full engine.
        agent_kwargs.update(agent_provider_kwargs(model=_llm_model))

        agent = AIAgent(**agent_kwargs)
        result = agent.chat("Generate.")
        return result.strip() if result else None

    except Exception as e:
        logger.warning("[social] _hermes_chat failed: %s", e)
        return None

# ════════════════════════════════════════════════════════════════════════════
#  RiveBot State Persistence Helpers
# ════════════════════════════════════════════════════════════════════════════

# In-process state cache — fallback when RiveBot is down.
# Keyed by (urn, var), pruned on write. Ensures scenario context
# survives from sim_get_scenario to sim_drill_grade within one process.
_local_state_cache: dict[tuple[str, str], str] = {}


def _get_urn(args: dict = None, **kw) -> str:
    """Extract the user URN from the invocation context.

    Priority:
      1. args['user_id'] — set by /v1/tools/ adapter (RapidPro flow webhooks)
      2. Hermes _current_urn — set during Hermes agent loop
      3. Fallback to 'user'
    """
    # Check args dict first (flow webhook path — tools adapter injects user_id)
    if args and isinstance(args, dict):
        uid = args.get("user_id", "")
        if uid and uid != "rivebot":
            return uid

    # Check Hermes context variable (agent loop path)
    try:
        from app.hermes.engine import _current_urn
        urn = _current_urn.get()
        if urn:
            return urn
    except Exception:
        pass
    return "user"


def _set_sim_var(urn: str, var: str, value: str) -> bool:
    """Persist a simulation variable in RiveBot + local cache."""
    _local_state_cache[(urn, var)] = str(value)
    try:
        httpx.post(
            f"{RIVEBOT_URL}/set-var",
            json={
                "persona": "social-code",
                "user": urn,
                "var": f"sim_{var}",
                "value": str(value),
            },
            timeout=2.0,
        )
        return True
    except Exception as e:
        logger.debug("RiveBot set failed for %s/%s, using local cache: %s", urn, var, e)
        return False


def _get_sim_var(urn: str, var: str, default: str = "") -> str:
    """Read a simulation variable — RiveBot first, local cache fallback."""
    val = _get_rivebot_var(urn, f"sim_{var}", "")
    if val and val != "undefined":
        return val
    # Fallback to local process cache
    return _local_state_cache.get((urn, var), default)


def _get_rivebot_var(urn: str, var: str, default: str = "") -> str:
    """Read any RiveBot user variable (no prefix manipulation)."""
    try:
        resp = httpx.get(
            f"{RIVEBOT_URL}/get-var",
            params={
                "persona": "social-code",
                "user": urn,
                "var": var,
            },
            timeout=2.0,
        )
        val = resp.json().get("value", default)
        return val if val != "undefined" else default
    except Exception:
        return default


# ════════════════════════════════════════════════════════════════════════════
#  Hermes-Registered Simulation Tools
# ════════════════════════════════════════════════════════════════════════════


@register_tool(
    name="sim_update_mood",
    description="Update the persona's mood during a social simulation.",
    trigger="",
)
def sim_update_mood(args: dict, **kw) -> str:
    """Update mood state and persist via RiveBot."""
    urn = _get_urn(args)
    new_mood = args.get("new_mood", "Neutral")
    internal = args.get("internal_thought", "")
    reason = args.get("reason", "")

    _set_sim_var(urn, "mood", new_mood)
    _set_sim_var(urn, "monologue", internal[:200])

    logger.info("[social] %s mood → %s (%s)", urn, new_mood, reason)
    return f"Mood updated to: {new_mood}"


@register_tool(
    name="sim_update_trust",
    description="Adjust trust score based on user's social behavior.",
    trigger="",
)
def sim_update_trust(args: dict, **kw) -> str:
    """Adjust trust and persist."""
    urn = _get_urn(args)
    change = int(args.get("trust_change", 0))
    reason = args.get("reason", "")

    current = int(_get_sim_var(urn, "trust", "20"))
    new_trust = max(0, min(100, current + change))
    _set_sim_var(urn, "trust", str(new_trust))

    direction = "gained" if change > 0 else "lost"
    logger.info("[social] %s trust %s %d → %d (%s)", urn, direction, current, new_trust, reason)
    return f"Trust score: {current} → {new_trust} ({'+' if change >= 0 else ''}{change})"


@register_tool(
    name="sim_update_dossier",
    description="Record a fact learned about the user.",
    trigger="",
)
def sim_update_dossier(args: dict, **kw) -> str:
    """Record a fact in the simulation dossier."""
    urn = _get_urn(args)
    key = args.get("key", "unknown")
    value = args.get("value", "")

    # Dossier is stored as a JSON string in RiveBot
    raw = _get_sim_var(urn, "dossier", "{}")
    try:
        dossier = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        dossier = {}

    dossier[key] = value
    _set_sim_var(urn, "dossier", json.dumps(dossier, ensure_ascii=False))

    logger.info("[social] %s dossier: %s = %s", urn, key, value)
    return f"Dossier updated: {key} = {value}"


@register_tool(
    name="sim_assess_boredom",
    description="Set the persona's engagement/boredom level.",
    trigger="",
)
def sim_assess_boredom(args: dict, **kw) -> str:
    """Set boredom level."""
    urn = _get_urn(args)
    level = max(0, min(10, int(args.get("boredom_level", 0))))
    reason = args.get("reason", "")

    _set_sim_var(urn, "boredom", str(level))

    status = "fascinated" if level < 3 else "engaged" if level < 6 else "bored" if level < 9 else "leaving"
    logger.info("[social] %s boredom %d (%s) — %s", urn, level, status, reason)
    return f"Boredom: {level}/10 ({status})"


@register_tool(
    name="sim_trigger_distraction",
    description="Introduce an environmental distraction.",
    trigger="",
)
def sim_trigger_distraction(args: dict, **kw) -> str:
    """Trigger an environmental distraction."""
    urn = _get_urn(args)
    level = max(0, min(10, int(args.get("distraction_level", 0))))
    source = args.get("source", "unknown")

    _set_sim_var(urn, "distraction", str(level))
    _set_sim_var(urn, "distraction_src", source[:100])

    logger.info("[social] %s distraction: %s (level %d)", urn, source, level)
    return f"Distraction triggered: {source} (Level {level})"


@register_tool(
    name="sim_grade_response",
    description="Grade the user's social skill response.",
    trigger="",
)
def sim_grade_response(args: dict, **kw) -> str:
    """Grade the user's response and format for WhatsApp."""
    skill = int(args.get("skill_score", 50))
    warmth = int(args.get("warmth_score", 50))
    critique = args.get("critique", "")
    better = args.get("better_version", "")
    wit = args.get("wit_mechanic", "None")

    # Format scorecard for WhatsApp
    skill_emoji = "🟢" if skill >= 80 else "🟡" if skill >= 60 else "🔴"
    warmth_emoji = "🟢" if warmth >= 80 else "🟡" if warmth >= 60 else "🔴"

    card = (
        f"📊 *Social Skills Scorecard*\n\n"
        f"{skill_emoji} *Skill*: {skill}/100\n"
        f"{warmth_emoji} *Warmth*: {warmth}/100\n"
    )
    if wit and wit != "None":
        card += f"🎭 *Wit*: {wit} mechanic detected\n"
    if critique:
        card += f"\n💡 *Feedback:* {critique}\n"
    if better:
        card += f"\n✨ *Try this:* _{better}_"

    return card


@register_tool(
    name="sim_get_scenario",
    description="Fetch a training scenario from the golden set.",
    trigger="",
)
def sim_get_scenario(args: dict, **kw) -> str:
    """Load a scenario — golden set or AI-generated when AI mode is on."""
    urn = _get_urn(args)
    difficulty = int(args.get("difficulty", 1))

    # Get language and AI mode from RiveBot context
    lang = _get_rivebot_var(urn, "lang", "en")
    if not lang or lang == "undefined":
        lang = "en"
    ai_enabled = _get_sim_var(urn, "ai_grading", "off").lower() == "on"

    # When AI is on, 30% chance of an AI-generated scenario for variety
    import random as _random
    use_ai_scenario = ai_enabled and _random.random() < 0.3

    if use_ai_scenario:
        ai_scenario = _generate_scenario_ai(difficulty, lang)
        if ai_scenario:
            _set_sim_var(urn, "current_context", ai_scenario["context"][:200])
            _set_sim_var(urn, "current_cue", ai_scenario["cue"][:200])
            _set_sim_var(urn, "current_persona", ai_scenario["target_persona"])
            _set_sim_var(urn, "difficulty", str(difficulty))

            import json as _json
            ideal_links = ai_scenario.get("ideal_links", [])
            if ideal_links:
                _set_sim_var(urn, "ideal_links", _json.dumps(ideal_links)[:500])
            else:
                _set_sim_var(urn, "ideal_links", "")

            return format_scenario_whatsapp(ai_scenario)

    # Default: golden set
    scenario = get_scenario(difficulty=difficulty, lang=lang)
    if not scenario:
        return "No scenarios available at this difficulty level."

    # Store current scenario context for grading reference
    _set_sim_var(urn, "current_context", scenario["context"][:200])
    _set_sim_var(urn, "current_cue", scenario["cue"][:200])
    _set_sim_var(urn, "current_persona", scenario["target_persona"])
    _set_sim_var(urn, "difficulty", str(difficulty))

    # Store golden ideal_links for use during grading
    import json as _json
    ideal_links = scenario.get("ideal_links", [])
    if ideal_links:
        _set_sim_var(urn, "ideal_links", _json.dumps(ideal_links)[:500])

    return format_scenario_whatsapp(scenario)


# ════════════════════════════════════════════════════════════════════════════
#  AI Scenario Generation
# ════════════════════════════════════════════════════════════════════════════

_SCENARIO_GEN_PROMPT = """Generate a social skills training scenario at difficulty level {difficulty}/4.

Level guide:
- Level 1: Casual everyday encounters (coffee shop, park, elevator)
- Level 2: Light workplace/social situations requiring tact
- Level 3: Challenging interpersonal conflicts, negotiations, or boundary-setting
- Level 4: High-stakes emotional situations (grief, confrontation, crisis)

Language: {lang_name}

Reply in this EXACT format (no extra text):
CONTEXT: <2-3 sentence scene description>
CUE: <what the other person says or does — 1 sentence>
PERSONA: <who the other person is — 2-3 words>
IDEAL: <your best response — 1-2 sentences>
STRATEGY: <strategy name — 2-3 words like 'Empathy', 'Humorous Pivot', 'Direct Challenge'>
EXPLANATION: <why this ideal response works — 1 sentence>"""

_LANG_NAMES = {"en": "English", "ht": "Kreyòl Ayisyen", "es": "Español", "fr": "Français"}


def _generate_scenario_ai(difficulty: int, lang: str = "en") -> dict | None:
    """Generate a fresh scenario using Hermes. Returns dict or None on failure."""
    try:
        text = _hermes_chat(
            _SCENARIO_GEN_PROMPT.format(
                difficulty=difficulty,
                lang_name=_LANG_NAMES.get(lang, "English"),
            ),
            urn="system:social-code",
        )
        if not text:
            return None

        # Parse structured response
        context = cue = persona = ideal = strategy = explanation = ""
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("CONTEXT:"):
                context = line.split(":", 1)[1].strip()
            elif line.upper().startswith("CUE:"):
                cue = line.split(":", 1)[1].strip()
            elif line.upper().startswith("PERSONA:"):
                persona = line.split(":", 1)[1].strip()
            elif line.upper().startswith("IDEAL:"):
                ideal = line.split(":", 1)[1].strip()
            elif line.upper().startswith("STRATEGY:"):
                strategy = line.split(":", 1)[1].strip()
            elif line.upper().startswith("EXPLANATION:"):
                explanation = line.split(":", 1)[1].strip()

        if not context or not cue:
            return None

        scenario = {
            "context": context,
            "cue": cue,
            "cue_category": "AI Generated",
            "difficulty": difficulty,
            "target_persona": persona or "Someone",
            "cultural_context": "",
            "tags": ["ai-generated"],
            "ideal_links": [],
        }

        if ideal:
            scenario["ideal_links"] = [{
                "angle_type": strategy or "AI Suggested",
                "link_text": ideal,
                "explanation": explanation or "",
            }]

        logger.info("[social] AI-generated scenario: %s", context[:60])
        return scenario

    except Exception as e:
        logger.warning("[social] AI scenario generation failed: %s", e)
        return None



# ════════════════════════════════════════════════════════════════════════════
#  AI Grading — LLM-based context-aware scoring
# ════════════════════════════════════════════════════════════════════════════

_AI_GRADE_PROMPT = """You are grading a social skills training response.

SCENARIO: {context}
USER RESPONSE: {user_input}
EXPERT IDEAL ({angle}): {ideal}

Score the user's response on two axes (0-100 each):
- SKILL: appropriateness, strategy, social intelligence for THIS specific scenario
- WARMTH: emotional connection, empathy, genuine human warmth

Provide a brief critique (2-3 bullet points, use ✅ for strengths and 📌 for areas to improve).
Optionally suggest a better response if score < 80. 
CRITICAL: The BETTER response MUST NOT be generic. It MUST directly apply the expert strategy ({angle}) and address the specific weaknesses noted in your critique. Keep it natural, conversational, and aligned with the curriculum.

Reply in this EXACT format (no extra text):
SKILL: <number>
WARMTH: <number>
CRITIQUE: <text>
BETTER: <text or NONE>"""


def _grade_with_ai(
    context: str, user_input: str,
    golden_ideal: str | None, golden_angle: str | None,
    lang: str = "en",
) -> tuple:
    """Grade a response via Hermes. Returns (skill, warmth, critique, better).

    Falls back to offline grading if Hermes is unavailable.
    """
    try:
        prompt = _AI_GRADE_PROMPT.format(
            context=context[:300],
            user_input=user_input[:500],
            angle=golden_angle or "General",
            ideal=golden_ideal or "N/A",
        )

        text = _hermes_chat(prompt, urn="system:social-code")
        if not text:
            raise RuntimeError("Empty Hermes response")

        # Parse structured response
        skill = 50
        warmth = 50
        critique = ""
        better = ""

        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("SKILL:"):
                try:
                    skill = int("".join(c for c in line.split(":", 1)[1] if c.isdigit())[:3])
                except (ValueError, IndexError):
                    pass
            elif line.upper().startswith("WARMTH:"):
                try:
                    warmth = int("".join(c for c in line.split(":", 1)[1] if c.isdigit())[:3])
                except (ValueError, IndexError):
                    pass
            elif line.upper().startswith("CRITIQUE:"):
                critique = line.split(":", 1)[1].strip()
            elif line.upper().startswith("BETTER:"):
                val = line.split(":", 1)[1].strip()
                if val.upper() != "NONE":
                    better = val

        skill = max(0, min(100, skill))
        warmth = max(0, min(100, warmth))

        return skill, warmth, critique, better

    except Exception as e:
        logger.warning("[social] AI grading failed, falling back to offline: %s", e)
        grade = analyze_response_offline(context, user_input, lang)
        return grade["score"], grade["warmth_score"], grade.get("critique", ""), ""


@register_tool(
    name="sim_drill_grade",
    description="Grade a user's drill response using offline analysis + state tracking. "
                "Called by the RapidPro flow webhook — not by Hermes directly.",
    trigger="",  # No RiveBot trigger — webhook-only
)
def sim_drill_grade(args: dict, **kw) -> str:
    """Full grading pipeline for flow webhooks.

    Two modes based on user's ai_grading preference:
      - Practice mode (default): qualitative feedback + golden ideals, no numeric scores
      - AI mode: LLM-graded numeric scores + FSRS scheduling
    """
    urn = _get_urn(args)
    user_input = args.get("user_input", args.get("critique", ""))

    if not user_input:
        return "⚠️ No response received."

    # ── Read current state from RiveBot ──
    context = _get_sim_var(urn, "current_context", "a social situation")
    current_mood = _get_sim_var(urn, "mood", "Neutral")
    current_trust = int(_get_sim_var(urn, "trust", "20"))
    current_boredom = int(_get_sim_var(urn, "boredom", "0"))
    lang = _get_rivebot_var(urn, "lang", "en") or "en"
    ai_enabled = _get_sim_var(urn, "ai_grading", "off").lower() == "on"

    # ── Read golden ideal_links from state ──
    import json as _json
    ideal_links_raw = _get_sim_var(urn, "ideal_links", "")
    golden_ideal = None
    golden_explanation = None
    golden_angle = None
    if ideal_links_raw and ideal_links_raw != "undefined":
        try:
            ideal_links = _json.loads(ideal_links_raw)
            if ideal_links and isinstance(ideal_links, list):
                link = ideal_links[0]
                golden_ideal = link.get("link_text", "")
                golden_explanation = link.get("explanation", "")
                golden_angle = link.get("angle_type", "")
        except (ValueError, KeyError):
            pass

    # Fallback: search the scenario bank by context if RiveBot state failed
    if not golden_ideal and context and context != "a social situation":
        try:
            bank = load_scenarios()
            for _lvl, scenarios in bank.items():
                for sc in scenarios:
                    sc_ctx = sc.get("context", "")
                    if isinstance(sc_ctx, dict):
                        sc_ctx = sc_ctx.get(lang, sc_ctx.get("en", ""))
                    if sc_ctx and context[:50] in sc_ctx:
                        links = sc.get("ideal_links", [])
                        if links:
                            link = links[0]
                            lt = link.get("link_text", "")
                            if isinstance(lt, dict):
                                lt = lt.get(lang, lt.get("en", ""))
                            expl = link.get("explanation", "")
                            if isinstance(expl, dict):
                                expl = expl.get(lang, expl.get("en", ""))
                            golden_ideal = lt
                            golden_explanation = expl
                            golden_angle = link.get("angle_type", "")
                        break
                if golden_ideal:
                    break
        except Exception:
            pass

    # ── Update mood & trust (always, both modes) ──
    sentiment = detect_sentiment(user_input, lang)
    trust_delta = compute_trust_delta(user_input, current_trust, lang)
    new_trust = max(0, min(100, current_trust + trust_delta))

    from app.plugins.social.offline import MOOD_TRANSITIONS
    new_mood = MOOD_TRANSITIONS.get((current_mood, sentiment), current_mood)

    word_count = len(user_input.split())
    new_boredom = max(0, min(10, current_boredom + (1 if word_count < 4 else -1)))

    _set_sim_var(urn, "mood", new_mood)
    _set_sim_var(urn, "trust", str(new_trust))
    _set_sim_var(urn, "boredom", str(new_boredom))

    # ══════════════════════════════════════════════════════════════════════
    #  AI MODE — LLM-graded numeric scores + FSRS
    # ══════════════════════════════════════════════════════════════════════
    if ai_enabled:
        skill, warmth, ai_critique, ai_better = _grade_with_ai(
            context, user_input, golden_ideal, golden_angle, lang
        )

        # FSRS tracking with AI-calibrated scores
        scenario_key = _get_sim_var(urn, "current_cue", "")[:80] or "unknown"
        app_slug = _get_sim_var(urn, "app", "")
        difficulty = int(_get_sim_var(urn, "difficulty", "1"))
        fsrs_result = record_drill(
            user_urn=urn, scenario_key=scenario_key, app_slug=app_slug,
            difficulty=difficulty, skill=skill, warmth=warmth, lang=lang,
        )

        skill_emoji = "🟢" if skill >= 80 else "🟡" if skill >= 60 else "🔴"
        warmth_emoji = "🟢" if warmth >= 80 else "🟡" if warmth >= 60 else "🔴"
        trust_dir = "📈" if trust_delta > 0 else "📉" if trust_delta < 0 else "➡️"
        fsrs_emoji = {1: "🔴", 2: "🟠", 3: "🟢", 4: "✨"}.get(fsrs_result["rating"], "⚪")

        card = (
            f"📊 *Scorecard* 🤖\n\n"
            f"{skill_emoji} *Skill*: {skill}/100\n"
            f"{warmth_emoji} *Warmth*: {warmth}/100\n"
            f"{trust_dir} *Trust*: {current_trust} → {new_trust}\n"
            f"😊 *Mood*: {current_mood} → {new_mood}\n"
            f"{fsrs_emoji} *Review*: {fsrs_result['label']} (next {fsrs_result['interval_text']})\n"
        )

        if ai_critique:
            card += f"\n{ai_critique}\n"

        # Show AI ideal or golden ideal
        if ai_better:
            card += f"\n✨ *Try this:* _{ai_better}_\n"
        if golden_ideal:
            card += f"\n🎯 *Expert approach ({golden_angle}):* _{golden_ideal}_\n"
            if golden_explanation:
                card += f"💡 _{golden_explanation}_\n"

        logger.info(
            "[social] %s AI grade: skill=%d warmth=%d trust=%d→%d mood=%s→%s fsrs=%s",
            urn, skill, warmth, current_trust, new_trust, current_mood, new_mood,
            fsrs_result['label'],
        )
        return card

    # ══════════════════════════════════════════════════════════════════════
    #  PRACTICE MODE (default) — qualitative feedback, no numeric scores
    # ══════════════════════════════════════════════════════════════════════
    grade = analyze_response_offline(context, user_input, lang)

    # Build qualitative card
    trust_dir = "📈" if trust_delta > 0 else "📉" if trust_delta < 0 else "➡️"
    card = (
        f"📋 *Feedback*\n\n"
        f"{trust_dir} *Trust*: {current_trust} → {new_trust}\n"
        f"😊 *Mood*: {current_mood} → {new_mood}\n"
    )

    if grade.get("critique"):
        card += f"\n{grade['critique']}\n"

    if golden_ideal:
        card += f"\n✨ *Expert approach ({golden_angle}):* _{golden_ideal}_\n"
        if golden_explanation:
            card += f"💡 _{golden_explanation}_\n"

    if new_boredom >= 8:
        card += "\n⚠️ The persona is getting bored! Try asking questions or sharing something personal."

    card += "\n_Enable 🤖 AI Grading in the menu for scored feedback + spaced repetition._"

    logger.info(
        "[social] %s practice grade: trust=%d→%d mood=%s→%s",
        urn, current_trust, new_trust, current_mood, new_mood,
    )
    return card


@register_tool(
    name="sim_freetext",
    description="Handle free-text input that doesn't match a menu option. "
                "Routes to RiveBot first; if no match, forwards to Hermes.",
    trigger="",
)
def sim_freetext(args: dict, **kw) -> str:
    """Conversational fallback for unrecognized menu input.

    Architecture: RiveBot first (deterministic), Hermes second (AI).
    Always returns the user to the menu they were on.
    """
    urn = _get_urn(args)
    user_input = args.get("user_input", "").strip()

    if not user_input:
        return "❓ I didn't catch that. Please pick an option from the menu."

    # 1. Try RiveBot first (deterministic match)
    try:
        resp = httpx.post(
            f"{RIVEBOT_URL}/reply",
            json={"persona": "social-code", "user": urn, "message": user_input},
            timeout=3.0,
        )
        rivebot_reply = resp.json().get("reply", "").strip()

        # RiveBot returns empty or its catchall "I don't understand" for non-matches
        if rivebot_reply and "i don't" not in rivebot_reply.lower() and len(rivebot_reply) > 5:
            return f"💬 {rivebot_reply}\n\n_Pick an option from the menu to continue._"
    except Exception as e:
        logger.debug("[sim_freetext] RiveBot unavailable: %s", e)

    # 2. Fall back to Hermes AI (lightweight one-shot prompt)
    try:
        ai_reply = _hermes_chat(
            f"You are a social skills training assistant on WhatsApp. "
            f"Answer the user's question briefly (2-3 sentences max). "
            f"User said: {user_input}",
            urn=urn,
        )
        if ai_reply:
            return f"🤖 {ai_reply}\n\n_Pick an option from the menu to continue._"
    except Exception as e:
        logger.debug("[sim_freetext] Hermes unavailable: %s", e)

    # 3. Final fallback
    return "❓ I didn't understand that. Please pick an option from the menu, or type *exit* to leave."


@register_tool(
    name="sim_set_language",
    description="Set the user's preferred language for Social-Code training.",
    trigger="",
)
def sim_set_language(args: dict, **kw) -> str:
    """Persist language preference for drill scenarios and feedback."""
    urn = _get_urn(args)
    lang_input = args.get("language", args.get("lang", "en")).strip().lower()

    # Normalize input to ISO code
    LANG_MAP = {
        "english": "en", "en": "en",
        "kreyòl": "ht", "kreol": "ht", "kreyol": "ht", "ht": "ht",
        "español": "es", "espanol": "es", "spanish": "es", "es": "es",
        "français": "fr", "francais": "fr", "french": "fr", "fr": "fr",
    }
    lang = LANG_MAP.get(lang_input, "en")
    lang_names = {"en": "English", "ht": "Kreyòl", "es": "Español", "fr": "Français"}

    # Persist in RiveBot state
    try:
        httpx.post(
            f"{RIVEBOT_URL}/set-var",
            json={"persona": "social-code", "user": urn, "var": "lang", "value": lang},
            timeout=2.0,
        )
    except Exception as e:
        logger.warning("[sim_set_language] Failed: %s", e)
        return "⚠️ Could not save language preference."

    return f"🌐 Language set to *{lang_names.get(lang, lang)}*.\n\nScenarios and feedback will now be in {lang_names.get(lang, lang)}."


@register_tool(
    name="sim_toggle_ai",
    description="Toggle AI-powered grading on/off for Social-Code training.",
    trigger="",
)
def sim_toggle_ai(args: dict, **kw) -> str:
    """Toggle AI grading mode. Off = qualitative feedback only. On = LLM scores + FSRS."""
    urn = _get_urn(args)

    # Read current state and toggle
    current = _get_sim_var(urn, "ai_grading", "off").lower()
    new_state = "off" if current == "on" else "on"

    _set_sim_var(urn, "ai_grading", new_state)

    if new_state == "on":
        return (
            "🤖 *AI Grading: ON*\n\n"
            "Your responses will now be scored by AI with:\n"
            "• 📊 Numeric Skill & Warmth scores (0-100)\n"
            "• 📅 Spaced repetition scheduling (FSRS)\n"
            "• 🎯 Context-aware feedback\n\n"
            "_Note: Requires internet connection._"
        )
    else:
        return (
            "📋 *AI Grading: OFF*\n\n"
            "You'll receive qualitative feedback:\n"
            "• ✅ What you did well\n"
            "• 📌 Areas to improve\n"
            "• ✨ Expert ideal responses\n\n"
            "_No numeric scores or internet needed._"
        )


@register_tool(
    name="sim_session_summary",
    description="Show a summary of the current training session.",
    trigger="",
)
def sim_session_summary(args: dict, **kw) -> str:
    """Aggregate session stats from the drill history database."""
    urn = _get_urn(args)
    stats = get_session_stats(urn, since_minutes=120)

    if stats["rounds"] == 0:
        return "📊 No drills completed in this session yet. Pick an app and start training!"

    avg_emoji = "🟢" if stats["avg_score"] >= 80 else "🟡" if stats["avg_score"] >= 60 else "🔴"

    return (
        f"📊 *Session Summary*\n\n"
        f"🔢 *Rounds*: {stats['rounds']}\n"
        f"{avg_emoji} *Avg Score*: {stats['avg_score']}/100\n"
        f"⚡ *Avg Skill*: {stats['avg_skill']}/100\n"
        f"💚 *Avg Warmth*: {stats['avg_warmth']}/100\n\n"
        f"_Type *train* to continue practicing!_"
    )
