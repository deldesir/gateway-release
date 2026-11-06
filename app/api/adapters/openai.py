"""
OpenAI-compatible adapter for RapidPro → AI Gateway communication.

RapidPro calls ``POST /v1/chat/completions`` with an OpenAI-style payload.
This adapter:
  1. Parses the RapidPro message prefix to extract URN + channel
  2. Resolves the channel to a persona
  3. Dispatches admin commands (/ or # prefixed)
  4. Invokes Hermes Agent and returns an OpenAI-shaped response
"""

import asyncio
import threading
import os
import time
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel

from app.api.middleware.message_parser import parse_rapidpro_message
from app.logger import logger
# NOTE: Legacy check_admin_permissions removed (ADR-011 migration).
# Auth is now handled by macro_bridge._verify_access() in rivebot.
from app.services.channel import resolve_persona, DEFAULT_PERSONA
from app.hermes.engine import invoke_hermes
from app.api.middleware.circuit_breaker import can_attempt, record_success, record_failure, status as breaker_status
from app.api.middleware.rate_limit import allow as _rate_allow

_RATE_LIMIT_TEXT = ("Trop de messages d'un coup, réessayez dans une minute. "
                    "Twòp mesaj an menm tan, eseye ankò nan yon minit.")

router = APIRouter(tags=["chat"])
api_logger = logger.bind(name="API")

# ── API Key Authentication (F-30) ────────────────────────────────────────────
# When GATEWAY_API_KEY is set, all requests must include it.
# When unset, dev mode — no auth required.
def _parse_authorized_users() -> dict:
    """Parse AUTHORIZED_USERS env var into a phone→name lookup dict."""
    raw = os.getenv("AUTHORIZED_USERS", "")
    if not raw:
        return {}
    result = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            phone, name = entry.split(":", 1)
            phone = phone.strip().replace("+", "").replace("whatsapp:", "")
            result[phone] = name.strip()
    return result

_AUTHORIZED_USERS: dict = _parse_authorized_users()

if not _AUTHORIZED_USERS:
    import logging as _gw_log
    _gw_log.getLogger(__name__).warning(
        "AUTHORIZED_USERS is not set — access gate disabled (open dev mode). "
        "Set AUTHORIZED_USERS=phone:Name,... to restrict access."
    )

_API_KEY = os.getenv("GATEWAY_API_KEY", "")


async def _verify_api_key(raw_request: Request):
    """Verify API key if configured. Skip in dev mode (no key set)."""
    if not _API_KEY:
        return  # Dev mode — no auth
    key = (
        raw_request.headers.get("X-API-Key")
        or raw_request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# In-memory cache: user_id → preferred persona slug
# Set when {{persona_switch}} fires, checked before channel default.
class _PersistedPrefs(dict):
    """The persona each person last chose or was switched to, kept on disk so a restart does
    not drop everyone back on the default persona. Best effort: a failed write is logged, never
    raised; a missing or unreadable file is an empty preference set."""

    def __init__(self, path: str):
        super().__init__()
        self._path = path
        try:
            import json as _json
            with open(path, encoding="utf-8") as fh:
                data = _json.load(fh)
            if isinstance(data, dict):
                self.update({str(k): str(v) for k, v in data.items() if isinstance(v, str)})
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            import json as _json
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump(dict(self), fh, ensure_ascii=False, indent=0, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"Persona preferences not saved: {e}")

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._save()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._save()


_user_persona: Dict[str, str] = _PersistedPrefs(os.getenv("PERSONA_PREFS_FILE", "data/persona_prefs.json"))


async def _talkprep_second_pass(user_id: str, message: str) -> Optional[str]:
    """The TalkPrep brain's reply to a line another persona's brain missed, for a person
    that brain admits; None for anyone else, for a miss, and for a persona switch the
    brain would propose (only a matched verb is followed from here)."""
    from app.graph.prompts import PersonaPromptRegistry

    persona = await PersonaPromptRegistry.get_async("talkprep") or {}
    if user_id not in (persona.get("allowed_urns") or []):
        return None
    from app.api.middleware.rivebot_client import match_intent

    reply, context = await match_intent(message, "talkprep", user_id)
    if reply is None or (context or {}).get("switch_persona"):
        return None
    return reply

# ── Session file persistence for cross-tier continuity (F-14) ─────────────────
import json
from pathlib import Path

_sessions_dir = Path(os.getenv(
    "HERMES_HOME", str(Path.home() / ".hermes")
)) / "sessions"


def _persist_rivebot_turn(user_id: str, persona: str, user_msg: str, bot_response: str):
    """Persist a RiveBot-handled turn into the SAME store Hermes reads.

    Cross-tier continuity only works if deterministic turns land in
    state.db: the old JSON session-file side-channel was consulted by
    _load_session_history ONLY when state.db had no rows for the session —
    i.e. never again after the user's first AI turn — and was written under
    the raw thread id while the reader normalized it. Net effect: the AI
    had amnesia for everything RiveBot handled. Writing through the shared
    SessionDB with the canonical session id closes that gap.
    """
    if not bot_response or bot_response.strip() in ("{{noreply}}", "{{silent}}"):
        return
    try:
        from app.hermes.engine import _get_session_db, get_session_id
        sid = get_session_id(user_id, persona)
        db = _get_session_db()
        db.ensure_session(sid, source="rivebot")
        db.append_message(sid, "user", user_msg)
        db.append_message(sid, "assistant", f"[RiveBot] {bot_response}")
    except Exception as e:
        api_logger.warning(f"Failed to persist RiveBot turn: {e}")

class OpenAIChatMessage(BaseModel):
    role: str
    content: str


class OpenAIChatRequest(BaseModel):
    model: str
    messages: List[Dict[str, str]]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    user: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _continue_after_change(user_id: str, persona: str, took: dict,
                                 system_prompt_override: str | None = None) -> str:
    """After a confirmed change ran, give the model one more turn with the result so a
    multi-step request goes on. The reply stays the tool's own line; the model's
    continuation is appended only when it has something to add — a further change
    becomes the next Wi/Non question, and a bare "nothing remains" is dropped."""
    import time as _t
    from app.graph.tools import confirm
    text = took["text"]
    if not can_attempt():
        return text
    try:
        from app.graph.prompts import PersonaPromptRegistry
        persona_vars = await PersonaPromptRegistry.get_async(persona)
        note = (
            f"[The person confirmed. The tool `{took['tool']}` ran ({took.get('label', '')}). "
            f"Result: {str(took.get('result') or '')[:1500]}]\n"
            "Continue with what the person originally asked, if anything remains to be done. "
            "If nothing remains, reply with exactly {{done}} and nothing else."
        )
        started = _t.time()
        result = await invoke_hermes(
            urn=user_id, persona=persona, message=note, system_prompt=system_prompt_override,
            rivebot_context={"lang": took.get("lang", "ht"), "echo_text": took.get("message", "")},
            persona_vars=persona_vars,
            allowed_tools=persona_vars.get("allowed_tools"), platform=_platform_for_urn(user_id),
        )
        more = (result.get("final_response") or "").strip() if isinstance(result, dict) else ""
        held = confirm.requested_since(user_id, started)
        if held:
            return text + "\n\n" + confirm.ask_text(held)
        if not more or "{{done}}" in more or _is_noreply(more) or result.get("timed_out"):
            return text
        return text + "\n\n" + more[:1500]
    except Exception as e:
        api_logger.warning(f"Continuation after a confirmed change failed for {user_id}: {e}")
        return text


def _openai_response(
    model: str,
    content: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    id_prefix: str = "chatcmpl",
) -> dict:
    """Build a well-formed OpenAI chat.completion response dict."""
    return {
        "id": f"{id_prefix}-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ── Responses API adapter ─────────────────────────────────────────────────────
# RapidPro/mailroom's native call_llm action calls the OpenAI *Responses* API
# (POST /v1/responses), NOT chat/completions. Adapt it onto the same chat logic
# so flows can reach the gateway via call_llm.
class ResponsesRequest(BaseModel):
    model: Optional[str] = "assistant"
    input: object  # str, or a list of Responses input items
    instructions: Optional[str] = None
    max_output_tokens: Optional[int] = None
    user: Optional[str] = None
    stream: Optional[bool] = False


def _responses_input_text(value) -> str:
    """Flatten the Responses 'input' (string or list of items) to plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                c = item.get("content")
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, list):
                    parts.extend(p.get("text", "") for p in c if isinstance(p, dict))
                elif "text" in item:
                    parts.append(item["text"])
        return "\n".join(p for p in parts if p)
    return str(value)


def _responses_envelope(model: str, text: str, prompt_tokens: int = 0,
                        completion_tokens: int = 0) -> dict:
    """Wrap a reply in the OpenAI Responses shape (parsed by goflow's OutputText)."""
    return {
        "id": f"resp-{uuid.uuid4()}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": "completed",
        "output": [{
            "type": "message",
            "id": f"msg-{uuid.uuid4()}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "output_text": text,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class EmbeddingsRequest(BaseModel):
    input: str | list[str]
    model: str = ""


_EMBED_FN = None
_EMBED_LOCK = threading.Lock()


def _embedding_function():
    # lazy so startup doesn't pay the model load; mempalace pins minilm via env
    global _EMBED_FN
    if _EMBED_FN is None:
        with _EMBED_LOCK:
            if _EMBED_FN is None:
                from mempalace.embedding import get_embedding_function
                _EMBED_FN = get_embedding_function()
    return _EMBED_FN


@router.post("/v1/embeddings")
@router.post("/embeddings")
async def openai_embeddings(request: EmbeddingsRequest) -> dict:
    """OpenAI-compatible embeddings endpoint backed by the local minilm model.

    Consumed by mailroom's knowledge-base indexing/search (MAILROOM_EMBEDDINGS_ENDPOINT).
    No API key: mailroom's embeddings client sends no Authorization header, and the
    port is loopback-bound and firewalled.
    """
    texts = [request.input] if isinstance(request.input, str) else list(request.input)
    if not texts:
        raise HTTPException(status_code=400, detail="input is required")

    ef = await asyncio.to_thread(_embedding_function)
    vectors = await asyncio.to_thread(ef, texts)
    return {
        "object": "list",
        "model": request.model or "minilm",
        "data": [
            {"object": "embedding", "index": i, "embedding": [float(x) for x in vec]}
            for i, vec in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@router.post("/v1/responses", dependencies=[Depends(_verify_api_key)])
@router.post("/responses", dependencies=[Depends(_verify_api_key)])
async def openai_responses(
    request: ResponsesRequest,
    raw_request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """OpenAI Responses-API endpoint consumed by RapidPro/mailroom call_llm."""
    import re
    text = _responses_input_text(request.input)
    # mailroom's call_llm cannot set the OpenAI `user` field, so the flow encodes
    # the sender's URN as a leading "scheme:path: " prefix on the input. Lift it
    # out as the user here (and strip it from the message the model sees).
    user = request.user
    if not user:
        m = re.match(r"^((?:tel|whatsapp|telegram|mailto):[^\s:]+):\s*", text)
        if m:
            user = m.group(1)
            text = text[m.end():]
    chat_req = OpenAIChatRequest(
        model=request.model or "assistant",
        messages=[{"role": "user", "content": text}],
        user=user,
    )
    chat = await openai_chat_completions(chat_req, raw_request, background_tasks)
    msg = (chat.get("choices") or [{}])[0].get("message", {}) or {}
    reply = msg.get("content", "") or ""
    usage = chat.get("usage", {}) or {}
    return _responses_envelope(
        request.model or "assistant", reply,
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
    )


_PROACTIVE_RECALL = (
    os.getenv("MEMPALACE_PALACE_PATH", "")
    and os.getenv("GATEWAY_PROACTIVE_RECALL", "1").strip().lower() not in ("0", "false", "no", "off")
)


def _recall_relevant_memories(
    user_id: str,
    message: str,
    limit: int = 3,
    min_similarity: float = 0.30,
    max_chars: int = 900,
) -> str:
    """Proactively recall relevant past drawers from the user's wing (ADR-009 F-03).

    Hermes can already call search_memory itself, but injecting the most relevant
    prior turns up front means continuity doesn't depend on the agent choosing to
    search. Tenant-scoped (per-urn wing), bounded, and best-effort — never raises.
    Returns a compact context block to append to the system prompt, or "".
    """
    if not _PROACTIVE_RECALL or not message.strip():
        return ""
    try:
        from mempalace.mcp_server import tool_search

        wing = f"wing_{user_id.split(':')[-1].lstrip('+')}"
        res = tool_search(query=message, wing=wing, limit=limit)
        if not isinstance(res, dict):
            return ""
        lines = []
        for h in res.get("results") or []:
            if (h.get("similarity") or 0) < min_similarity:
                continue
            text = (h.get("text") or "").strip().replace("\n", " ")
            if text:
                lines.append("- " + text[:300])
        if not lines:
            return ""
        block = (
            "Relevant context from this user's past conversations "
            "(use only if helpful; do not repeat verbatim):\n" + "\n".join(lines)
        )
        return block[:max_chars]
    except Exception as e:  # recall is best-effort — never block the turn
        api_logger.debug(f"Proactive recall failed for {user_id}: {e}")
        return ""


_NOREPLY = "{{noreply}}"


def _has_noreply_sentinel(text) -> bool:
    """True if the {{noreply}} handoff sentinel appears anywhere in text.

    Case- and whitespace-insensitive, so a wrapped or re-spaced sentinel
    (e.g. "Ok, {{ noreply }}") still matches and never leaks the literal token.
    Empty text is NOT a sentinel (an empty tool result is not a handoff).
    """
    s = (text if isinstance(text, str) else "").strip()
    return bool(s) and "{{noreply}}" in "".join(s.lower().split())


def _empty_reply_fallback(lang: str) -> str:
    """What an authorized user sees when the agent produced nothing at all."""
    if (lang or "ht").startswith("en"):
        return "I couldn't come up with an answer for that — try again, or ask it another way."
    if (lang or "").startswith("fr"):
        return "Je n'ai pas trouvé de réponse — réessaie, ou pose la question autrement."
    return "Mwen pa t rive jwenn yon repons pou sa — eseye ankò, oswa mande l yon lòt jan."


def _is_noreply(text) -> bool:
    """True when a final response is not deliverable: empty/whitespace, or it
    carries the {{noreply}} sentinel. Mirrors hermes-agent's [SILENT] match."""
    s = (text if isinstance(text, str) else "").strip()
    return (not s) or _has_noreply_sentinel(text)


def _tool_returned_noreply(hermes_result) -> bool:
    """True if any tool in the turn returned the {{noreply}} sentinel.

    Handoff tools (CRM-ops menu, direct WuzAPI button, flow takeover) own the
    reply, so suppress regardless of what the model emits afterwards — this makes
    suppression robust even if the LLM doesn't echo the sentinel verbatim.
    """
    if not isinstance(hermes_result, dict):
        return False
    for m in hermes_result.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "tool" and _has_noreply_sentinel(m.get("content")):
            return True
    return False


# URN scheme → Hermes platform key (drives the per-platform formatting hint).
# Default "whatsapp" preserves prior behavior; mailto → "email" so email replies
# are formatted for email (well-structured, plain text, MEDIA: attachments,
# subject threading) instead of WhatsApp's 4096-char/no-markdown constraints.
_SCHEME_TO_PLATFORM = {
    "whatsapp": "whatsapp",
    "mailto": "email",
    "tel": "sms",
    "telegram": "telegram",
    "discord": "discord",
    "slack": "slack",
    "signal": "signal",
}


def _platform_for_urn(urn: str) -> str:
    scheme = (urn or "").split(":", 1)[0].strip().lower()
    return _SCHEME_TO_PLATFORM.get(scheme, "whatsapp")


def _extract_usage(result: dict) -> tuple[int, int]:
    """Pull real token counts from the last AIMessage response_metadata."""
    messages = result.get("messages", [])
    if not messages:
        return 0, 0
    meta = getattr(messages[-1], "response_metadata", {}) or {}
    usage = meta.get("token_usage") or meta.get("usage") or {}
    return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/v1/chat/completions", dependencies=[Depends(_verify_api_key)])
@router.post("/chat/completions", dependencies=[Depends(_verify_api_key)])
async def openai_chat_completions(
    request: OpenAIChatRequest,
    raw_request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """OpenAI-compatible chat endpoint consumed by RapidPro's AI LLM config."""
    api_logger.info(f"Incoming request | model={request.model} | user={request.user}")
    api_logger.debug(f"Headers: {dict(raw_request.headers)}")

    if not request.messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    if len(request.messages) > 1:
        api_logger.warning(
            f"Received {len(request.messages)} messages in payload, using last only"
        )

    raw_content = request.messages[-1]["content"]

    # ── 1. Parse RapidPro message prefix ─────────────────────────────────────
    # RapidPro may include attachments in the last message or as a top-level field.
    raw_attachments = request.messages[-1].get("attachments", [])
    parsed = parse_rapidpro_message(raw_content, user_hint=request.user,
                                    attachments=raw_attachments)
    api_logger.info(
        f"Parsed | user={parsed.user_id} | channel={parsed.channel_id}"
        f" | content='{parsed.content[:60]}'"
    )

    if not parsed.user_id:
        api_logger.warning("Missing 'user' field in request — rejecting (F-10).")
        return _openai_response(
            DEFAULT_PERSONA, "{{noreply}}", id_prefix="chatcmpl-nouser"
        )

    # Update request with cleaned values
    request.messages[-1]["content"] = parsed.content
    if parsed.channel_id:
        request.model = parsed.channel_id

    user_id = parsed.user_id
    last_user_message = parsed.content

    # ── 1.6 Authorization gate (F-26) ────────────────────────────────────────
    # Check BEFORE RiveBot, commands, or Hermes — blocks all code paths.
    if _AUTHORIZED_USERS:
        user_digits = user_id.replace("+", "").split(":")[-1]
        if user_digits not in _AUTHORIZED_USERS:
            # Chat-granted users (any tier in the access grants file) are
            # authorized too — `access grant <phone> member` lifts the
            # silence without an env edit or restart.
            from app.graph.tools.access import is_granted
            if not is_granted(user_id):
                api_logger.warning(f"Unauthorized user {user_id} — silent drop")
                return _openai_response(
                    DEFAULT_PERSONA, "", id_prefix="chatcmpl-unauth"
                )

    # ── 1.7 Per-user rate limit ──────────────────────────────────────────────
    # After the gate (an unauthorized number never counts), before anything that costs:
    # a flood from one number gets a short notice at zero tokens, not the model.
    if not _rate_allow(user_id):
        api_logger.warning(f"Rate limit reached for {user_id}")
        return _openai_response(DEFAULT_PERSONA, _RATE_LIMIT_TEXT, id_prefix="chatcmpl-ratelimit")

    # ── 1.5  Attachment handling ─────────────────────────────────────────────
    if parsed.attachments:
        # Check if any attachment or the message text mentions .jwpub
        is_jwpub = ".jwpub" in last_user_message.lower()
        jwpub_url = None

        for att in parsed.attachments:
            att_str = att if isinstance(att, str) else str(att)
            # Check attachment URL/name for .jwpub
            if ".jwpub" in att_str.lower():
                is_jwpub = True
            # Extract URL from "content_type:http://..." format
            if is_jwpub and jwpub_url is None:
                # Find the URL portion (starts with http)
                http_idx = att_str.find("http")
                if http_idx >= 0:
                    jwpub_url = att_str[http_idx:]
                elif ":" in att_str:
                    # Fallback: split on first colon after mime type
                    parts = att_str.split(":", maxsplit=2)
                    jwpub_url = ":".join(parts[1:]) if len(parts) > 1 else att_str
                else:
                    jwpub_url = att_str

        if is_jwpub and jwpub_url:
            api_logger.info(f"JWPUB detected — routing to upload_jwpub (AI-free): {jwpub_url}")
            try:
                from app.graph.tools.upload import upload_jwpub
                result = await upload_jwpub.ainvoke({"media_url": jwpub_url})
            except Exception as e:
                result = f"❌ Error processing .jwpub file: {e}"

            # Auto-switch to talkprep persona after successful jwpub upload
            _user_persona[user_id] = "talkprep"

            # Persist this system action into the destination session
            # so the TalkPrep AI is aware that the presentation was just processed
            _persist_rivebot_turn(
                user_id, "talkprep",
                user_msg=f"[SysAction] User uploaded presentation: {jwpub_url}",
                bot_response=f"[SysAction] Upload result: {result}"
            )
            
            api_logger.info(f"Auto-switched {user_id} to talkprep persona after jwpub upload")

            return _openai_response(
                "talkprep", result,
                id_prefix="chatcmpl-jwpub",
            )

        # Non-jwpub attachments: inject context for AI
        att_lines = []
        for att in parsed.attachments:
            parts = att.split(":", 1) if isinstance(att, str) else []
            if len(parts) == 2:
                att_lines.append(f"[Attachment: {parts[0]} at {parts[1]}]")
            else:
                att_lines.append(f"[Attachment: {att}]")
        last_user_message = last_user_message + "\n" + "\n".join(att_lines)


    # ── 2. Resolve persona from user preference or channel config ─────────────
    # User preference (from previous persona switch) takes priority
    preferred = _user_persona.get(user_id)
    if preferred:
        model_persona, system_prompt_override = await resolve_persona(preferred)
        api_logger.info(f"Using preferred persona for {user_id}: {model_persona}")
    else:
        # First try resolving by user_id to hit the ChannelConfig table
        model_persona, system_prompt_override = await resolve_persona(user_id)
        
        # If it fell back to default, and request.model is provided and not generic, use it:
        if model_persona == DEFAULT_PERSONA and request.model and request.model != "custom_ai":
            model_persona, system_prompt_override = await resolve_persona(request.model)

        # Auto-upgrade: if user landed on the default persona but their URN is
        # in a privileged persona's allowed_urns, assign that persona
        # automatically.  This avoids forcing admins to say "Talk to assistant"
        # after every service restart (the in-memory cache is volatile).
        if model_persona == DEFAULT_PERSONA:
            try:
                from app.db import async_session as _as
                from app.models import Persona
                from sqlmodel import select as _select
                async with _as() as session:
                    result = await session.execute(
                        _select(Persona).where(Persona.slug == "assistant")
                    )
                    assistant_p = result.scalar_one_or_none()
                    if assistant_p and assistant_p.allowed_urns:
                        urns = assistant_p.allowed_urns
                        if isinstance(urns, str):
                            import json as _json
                            urns = _json.loads(urns)
                        api_logger.debug(
                            f"Auto-upgrade check: user_id={user_id!r} urns={urns!r}"
                        )
                        if user_id in urns:
                            model_persona = "assistant"
                            _user_persona[user_id] = "assistant"
                            api_logger.info(
                                f"Auto-upgraded {user_id} to assistant (allowed_urns match)"
                            )
            except Exception as e:
                api_logger.warning(f"allowed_urns auto-upgrade check failed: {e}")

    # Talk commands ("use talk 50", "talk 50", "diskou 50") belong to TalkPrep,
    # not the general assistant. Whenever one would land on the assistant persona
    # (cached OR auto-upgraded this turn), switch + persist to talkprep so it is
    # handled directly instead of the assistant floundering on a request it does
    # not own.
    import re as _talk_re
    if model_persona == "assistant" and _talk_re.match(
        r"^\s*(sèvi\s+ak\s+)?(use\s+)?(talk|diskou)\s+\d+",
        last_user_message or "", _talk_re.I,
    ):
        model_persona, system_prompt_override = await resolve_persona("talkprep")
        _user_persona[user_id] = "talkprep"
        api_logger.info(f"Routing talk command to talkprep persona for {user_id}")

    # ── 3. Build persona-scoped thread ID ─────────────────────────────────────
    thread_id = f"whatsapp:{user_id}:{model_persona}"

    # Admin commands flow through RiveBot (system.rive → macro_bridge →
    # gateway tools); user promotion through the access macro (access.py).

    # ── 4.5 Deterministic intent router (zero tokens) ────────────────────────
    # ── 4.4 Deterministic capture route (zero tokens) ────────────────────────
    # An operator jot that starts with a capture verb ("sonje: …", "tiket: …")
    # is filed into its sink here, before the router and before any model
    # turn. The verb decided what the line is; a model never gets to decide
    # whether a reminder or a ticket is worth keeping.
    # ── 4.3 Confirmation of a held change (zero tokens) ──────────────────────
    # A *Wi*/*Non* while a model-proposed change is waiting runs or drops that
    # change here, before the router and before any model; the stored call runs
    # exactly as stored and the reply is the tool's own.
    from app.graph.tools import confirm
    took = await asyncio.to_thread(confirm.take, user_id, last_user_message)
    if took is not None:
        confirm_reply = took["text"]
        if took.get("executed"):
            # The change ran. If it was one step of a longer request, one more model
            # turn carries on from the result; a next change is held and asked again.
            confirm_reply = await _continue_after_change(
                user_id, model_persona, took, system_prompt_override)
        _persist_rivebot_turn(user_id, model_persona, last_user_message, confirm_reply)
        api_logger.info(f"Held change answered deterministically for {user_id}")
        return _openai_response(model_persona, confirm_reply, id_prefix="chatcmpl-confirm")

    from app.graph.tools.capture import is_operator_capture, macro_capture
    if is_operator_capture(user_id, last_user_message):
        capture_reply = macro_capture({"text": last_user_message, "user_id": user_id})
        api_logger.info(f"Capture filed deterministically for {user_id}")
        return _openai_response(model_persona, capture_reply, id_prefix="chatcmpl-capture")

    from app.api.middleware.rivebot_client import match_intent, set_var, set_vars
    intent_response, rivebot_context = None, {}
    try:
        # Inject WhatsApp contact name if available (skips onboarding name question)
        if parsed.contact_name:
            try:
                await set_vars(model_persona, user_id, {
                    "name": parsed.contact_name,
                    "onboarded": "true",
                    "welcomed": "true",
                })
            except Exception:
                pass  # non-critical: onboarding will ask for name instead

        # F-6: Strip leading punctuation that breaks RiveBot matchers
        clean_message = last_user_message.lstrip('#/')
        intent_response, rivebot_context = await match_intent(
            clean_message, model_persona, user_id
        )
        rivebot_context["urn"] = user_id
        # (noai/silent handling removed — replaced by circuit breaker in §5)

        # Persona switch: re-route to new persona
        if rivebot_context.get("switch_persona"):
            new_slug = rivebot_context["switch_persona"]

            # ── Permission check (Finding 15) ────────────────────────────────
            # If the target persona has allowed_urns set, verify the user is authorized
            try:
                from app.db import async_session
                from app.models import Persona
                from sqlmodel import select

                async with async_session() as session:
                    result = await session.execute(
                        select(Persona).where(Persona.slug == new_slug)
                    )
                    target_persona = result.scalar_one_or_none()
                    if target_persona and target_persona.allowed_urns:
                        if user_id not in target_persona.allowed_urns:
                            api_logger.warning(
                                f"Persona switch DENIED: {user_id} → {new_slug} "
                                f"(not in allowed_urns)"
                            )
                            return _openai_response(
                                model_persona,
                                "⚠️ Ou pa gen aksè nan sèvis sa a.",
                                id_prefix="chatcmpl-denied",
                            )
            except Exception as e:
                # Fail CLOSED: an unverifiable switch could land in a
                # privileged persona (assistant = terminal/config toolsets).
                api_logger.warning(f"Persona permission check failed (denying): {e}")
                return _openai_response(
                    model_persona,
                    "⚠️ Ou pa gen aksè nan sèvis sa a.",
                    id_prefix="chatcmpl-denied",
                )

            _user_persona[user_id] = new_slug  # Persist preference
            model_persona = new_slug
            thread_id = f"whatsapp:{user_id}:{new_slug}"
            api_logger.info(f"Persona switch: {user_id} → {new_slug} (preference saved)")

            # NOTE: Global user state (lang, name, onboarded, welcomed) is auto-propagated
            # across all persona engines by RiveBot's set_uservar() — no manual carry-over needed.

            # Send a greeting via the new persona
            try:
                greet_resp, _ = await match_intent("bonjou", new_slug, user_id)
                if greet_resp:
                    return _openai_response(
                        new_slug, f"🔄 {greet_resp}", id_prefix="chatcmpl-switch"
                    )
            except Exception:
                pass
            return _openai_response(
                new_slug,
                "🔄 Pase nan " + new_slug.replace("-", " ").title() + ". Kijan m ka ede w?",
                id_prefix="chatcmpl-switch",
            )

        if intent_response is not None:
            # Best-effort: react to greetings/thanks even when sending a text reply
            if parsed.external_msg_id and parsed.user_id:
                _msg = last_user_message.strip().lower()
                _greeting_reactions = {
                    "hello": "👋🏾", "hi": "👋🏾", "hey": "👋🏾",
                    "bonjou": "👋🏾", "salut": "👋🏾", "alo": "👋🏾",
                    "thanks": "🙏🏾", "thank you": "🙏🏾", "mesi": "🙏🏾", "merci": "🙏🏾",
                    "bye": "👋🏾", "orevwa": "👋🏾", "kenbe": "👋🏾",
                }
                react_emoji = _greeting_reactions.get(_msg)
                if react_emoji:
                    phone = parsed.user_id.split(":")[-1].lstrip("+")
                    try:
                        from app.api.middleware.wuzapi_client import send_reaction
                        await send_reaction(phone, parsed.external_msg_id, react_emoji)
                    except Exception:
                        pass
            # ── Persist RiveBot turn to state.db (F-14) ─────────────────
            _persist_rivebot_turn(
                user_id, model_persona, last_user_message, intent_response
            )

            return _openai_response(
                model_persona, intent_response, id_prefix="chatcmpl-rs"
            )
    except Exception as e:
        logger.error(f"Rivebot match error: {e}")
        # On error, safely fall through to Hermes Agent

    # ── 4.5 The talk-preparation brain, asked second (zero tokens) ───────────
    # The loop's verbs live in the TalkPrep brain. Typed on another persona by a
    # person that brain admits ("prepare diskou 75 pou 2026-12-27", "defi seksyon
    # 1"), they are matched there before any model turn and the persona follows
    # the verb; a line the brain does not know falls through unchanged.
    if intent_response is None and model_persona != "talkprep":
        try:
            second = await _talkprep_second_pass(user_id, last_user_message.lstrip("#/"))
        except Exception as e:
            api_logger.warning(f"TalkPrep second pass failed for {user_id}: {e}")
            second = None
        if second is not None:
            model_persona = "talkprep"
            _user_persona[user_id] = "talkprep"
            _persist_rivebot_turn(user_id, model_persona, last_user_message, second)
            api_logger.info(f"TalkPrep verb matched on the second pass for {user_id}")
            return _openai_response(model_persona, second, id_prefix="chatcmpl-rs")

    # ── 4.6 Closed-set intent for what the router missed ─────────────────────
    # One small-model call that can only pick from the known verbs (or "chat",
    # or "unsure"). A verb is replayed through the router as if typed exactly,
    # so the rule executes, never the model; "unsure" asks with two buttons.
    try:
        from app.graph.tools import intent as _intent
        _routed = await _intent.route(user_id, last_user_message, model_persona,
                                      rivebot_context.get("lang", "ht"))
    except Exception as e:
        api_logger.warning(f"Intent routing failed for {user_id}: {e}")
        _routed = None
    if _routed and _routed.get("reply") is not None:
        if _routed.get("persona") and _routed["persona"] != model_persona:
            model_persona = _routed["persona"]
            _user_persona[user_id] = model_persona
        _persist_rivebot_turn(user_id, model_persona, last_user_message, _routed["reply"])
        return _openai_response(model_persona, _routed["reply"], id_prefix="chatcmpl-intent")
    if _routed and _routed.get("message"):
        last_user_message = _routed["message"]     # "just chat": the original line, to the model

    # ── 5. Hermes Agent invocation ───────────────────────────────────────────────

    # ── 5.0 Circuit breaker gate ──────────────────────────────────────────────
    if not can_attempt():
        api_logger.warning(f"Circuit breaker OPEN — skipping Hermes for {user_id}")
        lang = rivebot_context.get("lang", "ht")
        if lang == "en":
            degraded = (
                "⚠️ Our AI service is temporarily unavailable. We're working on it. "
                "In the meantime, type *help* to see what I can do for you."
            )
        else:
            degraded = (
                "⚠️ Sèvis AI nou an pa disponib pou kounye a. N ap travay sou sa. "
                "Antretan, tape *help* pou wè sa m ka fè pou ou."
            )
        return _openai_response(model_persona, degraded, id_prefix="chatcmpl-breaker")

    try:
        # Resolve persona properties
        from app.graph.prompts import PersonaPromptRegistry
        persona_vars = await PersonaPromptRegistry.get_async(model_persona)

        # Check allowed_urns for resolved persona (not just switches)
        if persona_vars.get("allowed_urns"):
            if user_id not in persona_vars["allowed_urns"]:
                api_logger.warning(f"Persona access DENIED: {user_id} → {model_persona}")
                return _openai_response(
                    model_persona,
                    "⚠️ Ou pa gen aksè nan sèvis sa a.",
                    id_prefix="chatcmpl-denied",
                )

        # ── Tier 3 Reaction Plumbing (Finding 22) ──────────────
        _phone = None
        _msg_id = None
        if _platform_for_urn(user_id) == "whatsapp":
            _phone = user_id.split(":")[-1].lstrip("+")
            # typing indicator while the model works — WhatsApp clears it when
            # the reply lands; cleared explicitly on failure and no-reply turns
            try:
                from app.api.middleware.wuzapi_client import send_presence
                asyncio.create_task(send_presence(_phone, "composing"))
            except Exception as e:
                api_logger.debug(f"WuzAPI presence init failed: {e}")
        if parsed.external_msg_id and _phone:
            _msg_id = parsed.external_msg_id
            try:
                from app.api.middleware.wuzapi_client import send_reaction
                asyncio.create_task(send_reaction(_phone, _msg_id, "⏳"))
            except Exception as e:
                api_logger.debug(f"WuzAPI reaction init failed: {e}")

        # Proactive memory recall (F-03): inject relevant past turns into the
        # system prompt so continuity doesn't depend on the agent searching.
        _recall = await asyncio.to_thread(
            _recall_relevant_memories, user_id, last_user_message
        )
        _system_prompt = (
            (system_prompt_override or "") + "\n\n" + _recall
            if _recall else system_prompt_override
        )
        if _recall:
            api_logger.info(f"Injected proactive recall context for {user_id} ({len(_recall)} chars)")

        import time as _time
        _turn_started = _time.time()
        hermes_result = await invoke_hermes(
            urn=user_id,
            persona=model_persona,
            message=last_user_message,
            system_prompt=_system_prompt,
            rivebot_context=rivebot_context,
            persona_vars=persona_vars,
            allowed_tools=persona_vars.get("allowed_tools"),
            platform=_platform_for_urn(user_id),
        )
        final_text = hermes_result.get("final_response", "")

        # A change the model proposed during this turn is waiting for the person's
        # tap: the question, with its two buttons, is the reply — not the model's
        # account of it. Deterministic wording; the model's text is dropped.
        _held = confirm.requested_since(user_id, _turn_started)
        if _held:
            final_text = confirm.ask_text(_held)
            api_logger.info(f"Change held for confirmation ({_held['tool']}) for {user_id}")

        # Clear the hourglass
        if _phone and _msg_id:
            try:
                from app.api.middleware.wuzapi_client import send_reaction
                asyncio.create_task(send_reaction(_phone, _msg_id, ""))
            except Exception:
                pass

        # ── AI succeeded — close circuit ──────────────────────────────────────
        record_success()

    except Exception as e:
        # ── AI failure — record in circuit breaker ────────────────────────────
        api_logger.error(f"Hermes failed for {user_id}: {e}")
        record_failure(str(e)[:200])

        # Clear the hourglass and the typing indicator on failure
        try:
            if '_phone' in locals() and _phone:
                from app.api.middleware.wuzapi_client import send_presence, send_reaction
                asyncio.create_task(send_presence(_phone, "paused"))
                if '_msg_id' in locals() and _msg_id:
                    asyncio.create_task(send_reaction(_phone, _msg_id, ""))
        except Exception:
            pass

        lang = rivebot_context.get("lang", "ht")
        if lang == "en":
            noai_msg = (
                "⚠️ Our AI service is temporarily unavailable. We're working on it. "
                "In the meantime, type *help* to see what I can do for you."
            )
        else:
            noai_msg = (
                "⚠️ Sèvis AI nou an pa disponib pou kounye a. N ap travay sou sa. "
                "Antretan, tape *help* pou wè sa m ka fè pou ou."
            )
        return _openai_response(model_persona, noai_msg, id_prefix="chatcmpl-noai")

    # ── 5.1 Silent / no-reply chokepoint ─────────────────────────────────
    # Single authoritative place that enforces suppression. If Hermes produced
    # no deliverable reply (empty) or a {{noreply}} handoff sentinel (possibly
    # wrapped by the model), the final return below emits EXACTLY {{noreply}} so
    # downstream suppression (RiveBot/flow) stays reliable and a wrapped sentinel
    # never leaks. Suppressed turns are not persisted to MemPalace. Also fires
    # when a TOOL returned the sentinel (handoff), even if the model didn't echo it.
    # An EMPTY answer that is NOT a handoff (no {{noreply}} anywhere, no tool
    # took over) is a failure, not silence: the agent ran out of budget or
    # produced nothing. A person who asked a question gets told so, briefly,
    # in their language — never dead air. Not persisted (nothing to remember).
    _empty_failure = (
        not (final_text or "").strip()
        and not _tool_returned_noreply(hermes_result)
    )
    if _empty_failure:
        api_logger.warning(f"Empty non-handoff reply for {user_id} — sending fallback")
        final_text = _empty_reply_fallback(rivebot_context.get("lang", "ht"))
        _skip_persist = True
    else:
        _skip_persist = bool(hermes_result.get("skip_persistence")) if isinstance(hermes_result, dict) else False

    _suppress = _is_noreply(final_text) or _tool_returned_noreply(hermes_result)
    if _suppress and '_phone' in locals() and _phone:
        # no reply is coming — don't leave the contact staring at "typing…"
        try:
            from app.api.middleware.wuzapi_client import send_presence
            asyncio.create_task(send_presence(_phone, "paused"))
        except Exception:
            pass

    # ── 5.2 Persistence (fire-and-forget — F-08) ─────────────────────────
    # Palace write runs in background to avoid blocking the HTTP response.
    if not _suppress and not _skip_persist and not getattr(final_text, "skip_persistence", False):
        from app.hooks.palace_writer import persist_turn_to_palace

        async def _safe_persist():
            try:
                await asyncio.to_thread(
                    persist_turn_to_palace,
                    urn=user_id,
                    persona=model_persona,
                    user_message=last_user_message,
                    assistant_response=final_text,
                )
            except Exception as e:
                api_logger.warning(f"Background palace write failed: {e}")

        asyncio.create_task(_safe_persist())

    # Token usage from Hermes (engine attaches per-turn usage from the agent's
    # session_* counters — works for the native-Gemini path). Guard for the
    # failure path where hermes_result may not be set.
    _usage = {}
    if "hermes_result" in locals() and isinstance(hermes_result, dict):
        _usage = hermes_result.get("usage") or {}
    prompt_tokens = int(_usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(_usage.get("completion_tokens", 0) or 0)

    if _suppress:
        api_logger.info(f"Suppressing reply for {user_id} (silent/handoff sentinel or empty)")
        return _openai_response(
            model_persona, _NOREPLY, prompt_tokens, completion_tokens,
            id_prefix="chatcmpl-noreply",
        )

    return _openai_response(
        model_persona, final_text, prompt_tokens, completion_tokens
    )
