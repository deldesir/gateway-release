from typing import Any, Dict, Optional
import asyncio
import json
import time

from fastapi import APIRouter, Header, HTTPException, Request, Depends
from loguru import logger
import os

from app.hermes.tools import get_hermes_tools  # Ensures tools are registered

router = APIRouter(tags=["tools"])

# CRM tools that benefit from typing indicators (ADR-011 T2)
_CRM_TOOLS = {"crm_list_groups", "crm_lookup_contact", "crm_org_info", "crm_create_group"}

INTERNAL_API_KEY = os.getenv("GATEWAY_INTERNAL_KEY", "")

# ── Caller authorization (persona toolset grants) ────────────────────────────
# The internal key proves the REQUEST came from a trusted service (RiveBot,
# RapidPro flows), not WHO the end user is — X-User-Id carries the caller's
# URN. Flows fire for whoever messages the channel, so without this check a
# stranger texting a keyword could reach admin toolsets (talkprep, siyuan…).
#
# Authorization = union of allowed_tools across every persona whose
# allowed_urns grants the caller's URN; callers granted nowhere get the
# default persona's toolsets. Persona selection stays a chat-layer concern —
# at this API layer only the union of grants matters. Fail-closed: a persona
# table read failure denies everything except the (empty) fallback.

_TOOLSET_CACHE: Dict[str, tuple] = {}  # user_id -> (monotonic_ts, set(toolsets))
_TOOLSET_CACHE_TTL = 60.0


def _norm_urn(urn: str) -> str:
    """Comparable identity: scheme stripped, '+' stripped, lowercased."""
    return urn.split(":")[-1].lstrip("+").strip().lower()


async def _caller_toolsets(user_id: str) -> set:
    now = time.monotonic()
    cached = _TOOLSET_CACHE.get(user_id)
    if cached and now - cached[0] < _TOOLSET_CACHE_TTL:
        return cached[1]

    from app.db import async_session
    from app.models import Persona
    from app.services.channel import DEFAULT_PERSONA
    from sqlmodel import select

    granted: set = set()
    fallback: set = set()
    caller = _norm_urn(user_id)
    try:
        async with async_session() as session:
            result = await session.execute(select(Persona))
            for p in result.scalars():
                urns = p.allowed_urns or []
                if isinstance(urns, str):
                    try:
                        urns = json.loads(urns)
                    except ValueError:
                        urns = []
                if p.slug == DEFAULT_PERSONA:
                    fallback = set(p.allowed_tools or [])
                if any(_norm_urn(u) == caller for u in urns):
                    granted |= set(p.allowed_tools or [])
    except Exception as e:
        logger.error(f"[tools] persona grant lookup failed for {user_id}: {e}")
        return set()  # fail closed

    # Chat-granted tiers (access grants file) extend the persona grants —
    # e.g. an "admin"-tier user reaches the talkprep toolset his menus need.
    try:
        from app.graph.tools.access import tier_toolsets
        granted |= tier_toolsets(user_id)
    except Exception as e:
        logger.warning(f"[tools] tier grant lookup failed for {user_id}: {e}")

    # The retail line: a number bound to a shop reaches the retail tools, whatever its persona.
    try:
        from app.graph.tools.retail import binding_for
        if binding_for(user_id):
            granted = set(granted) | {"retail"}
    except Exception as e:
        logger.warning(f"[tools] retail binding lookup failed for {user_id}: {e}")

    toolsets = granted or fallback
    _TOOLSET_CACHE[user_id] = (now, toolsets)
    return toolsets


# Tools ANY identified caller may invoke, regardless of persona grants.
# These back RiveBot's user-self (T3) capabilities: a customer submitting a
# support ticket / product request, and the self-service AI toggles. Keep
# this list short and boring — everything here runs with the CALLER's own
# identity and touches only their own state (or notifies the operator).
_PUBLIC_TOOLS = {
    "submit_form", "macro_reset", "macro_debug", "macro_noai", "macro_enableai",
    # the contact-directory menu relayed by a RapidPro flow: authorizes the caller
    # itself against their directory grants, and only ever serves them their own
    "contact_sync_dialog",
}


def _mask(user_id: str) -> str:
    """A caller for the log: scheme kept, identity reduced to its last four
    characters. The tools API serves every message on a public line, so its
    log must not become a list of who wrote."""
    scheme, _, value = (user_id or "").rpartition(":") if ":" in (user_id or "") else ("", "", user_id or "")
    if not value:
        return "***"
    tail = value[-4:] if len(value) > 4 else ""
    return f"{scheme}:***{tail}" if scheme else f"***{tail}"


async def _authorize_caller(tool_name: str, tool_entry, user_id: str) -> None:
    if tool_name in _PUBLIC_TOOLS:
        return
    toolset = getattr(tool_entry, "toolset", None)
    if not toolset:
        return
    allowed = await _caller_toolsets(user_id)
    if toolset not in allowed:
        logger.warning(
            f"[tools] DENIED {tool_name} for {_mask(user_id)} "
            f"(toolset '{toolset}' not granted; allowed={sorted(allowed)})"
        )
        raise HTTPException(
            status_code=403,
            detail=f"Tool '{tool_name}' is not available to this caller",
        )

async def verify_internal_key(x_api_key: str = Header(None, alias="X-API-Key")):
    """Validate that the caller is an authorized internal service (RiveBot).

    When GATEWAY_INTERNAL_KEY is not set, auth is skipped (dev mode) —
    matching the pattern in openai.py's _verify_api_key.
    """
    if not INTERNAL_API_KEY:
        return  # Dev mode — no auth required
    if x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid internal API key")


async def _invoke_tool(tool_name: str, kwargs: Dict[str, Any], user_id: str,
                       context: Optional[Dict[str, str]] = None) -> str:
    """Resolve and invoke a registered tool. Returns the string result.

    Positional args support: if kwargs contains '_args' (a list of strings),
    they are mapped positionally to the tool's Pydantic schema field names.
    This lets macro_bridge pass space-split RiveScript args without knowing
    each tool's parameter names at the .rive authoring time.
    """
    tools = get_hermes_tools()
    tool_entry = tools.get(tool_name)

    if not tool_entry:
        raise HTTPException(status_code=404, detail=f"Unknown tool: '{tool_name}'")

    await _authorize_caller(tool_name, tool_entry, user_id)

    if "_args" in kwargs:
        pos_args: list = kwargs.pop("_args")
        schema = getattr(tool_entry, "schema", {})
        props = schema.get("parameters", {}).get("properties", {})
        
        # Exclude auto-injected active_* fields from positional mapping
        field_names = [k for k in props.keys() if not k.startswith("active_")]

        for i, val in enumerate(pos_args):
            if i < len(field_names) - 1:
                kwargs.setdefault(field_names[i], val)
            elif i == len(field_names) - 1:
                kwargs.setdefault(field_names[i], " ".join(pos_args[i:]))
                break

    kwargs.setdefault("user_id", user_id)

    if context:
        for key, val in context.items():
            kwargs.setdefault(key, val)

    # ADR-011 T2: Show "typing..." for CRM L2 commands (fire-and-forget)
    if tool_name in _CRM_TOOLS and user_id:
        try:
            from app.api.middleware.wuzapi_client import send_presence
            phone = user_id.split(":")[-1].lstrip("+") if ":" in user_id else user_id
            asyncio.create_task(send_presence(phone, "composing"))
        except Exception:
            pass  # best-effort — don't block the tool call

    # In-handler gates (_is_admin_caller, _palace_wing, send_email's caller
    # read) resolve identity from the _current_urn contextvar, which only the
    # Hermes engine used to set. Set it here too so both invocation paths see
    # the same caller — otherwise mempalace tools scope to a shared "default"
    # wing and admin checks can't recognize the admin.
    from app.hermes.engine import _current_urn
    urn_token = _current_urn.set(user_id if ":" in user_id else "")

    try:
        # Hermes handlers are synchronous, we wrap them so they don't block RiveBot requests
        handler = getattr(tool_entry, "handler")
        result = await asyncio.to_thread(handler, kwargs if kwargs else {"user_id": user_id})
    except Exception as e:
        logger.error(f"[tools] {tool_name} raised {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Tool '{tool_name}' failed: {e}")
    finally:
        _current_urn.reset(urn_token)

    return _sanitize_text(str(result))


def _sanitize_text(text: str) -> str:
    """Make a tool's string result safe to JSON-serialize.

    Astral-plane emoji can slip into source or data as UTF-16 surrogate
    pairs (e.g. an editor writing 📄 as \\ud83d\\udcc4). Those are valid
    UTF-16 but not valid UTF-8, so FastAPI's JSON encoder raises and the
    caller gets a 500. Re-pairing via UTF-16 restores the real codepoint;
    anything still unpaired is dropped so a response is always returned.
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")



@router.post("/v1/tools/{tool_name}", dependencies=[Depends(verify_internal_key)])
async def call_tool_post(
    tool_name: str,
    request: Request,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_active_pub: Optional[str] = Header(None, alias="X-Active-Pub"),
    x_active_talk_id: Optional[str] = Header(None, alias="X-Active-Talk-Id"),
    x_active_revision: Optional[str] = Header(None, alias="X-Active-Revision"),
) -> dict:
    """
    Invoke a tool by name, passing JSON body kwargs.
    Called by RiveBot macro_bridge for tools that need arguments.
    """
    user_id = x_user_id or "rivebot"
    context = {}
    if x_active_pub: context["active_pub"] = x_active_pub
    if x_active_talk_id: context["active_talk_id"] = x_active_talk_id
    if x_active_revision: context["active_revision"] = x_active_revision

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    logger.info(f"[tools] POST {tool_name} | user={_mask(user_id)} | kwargs={list(body.keys())} | ctx={context}")
    result = await _invoke_tool(tool_name, body, user_id, context)
    return {"result": result}


@router.get("/v1/tools/{tool_name}", dependencies=[Depends(verify_internal_key)])
async def call_tool_get(
    tool_name: str,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_active_pub: Optional[str] = Header(None, alias="X-Active-Pub"),
    x_active_talk_id: Optional[str] = Header(None, alias="X-Active-Talk-Id"),
    x_active_revision: Optional[str] = Header(None, alias="X-Active-Revision"),
) -> dict:
    """
    Invoke a no-argument tool by name.
    Called by RiveBot macro_bridge for simple status/list tools.
    """
    user_id = x_user_id or "rivebot"
    context = {}
    if x_active_pub: context["active_pub"] = x_active_pub
    if x_active_talk_id: context["active_talk_id"] = x_active_talk_id
    if x_active_revision: context["active_revision"] = x_active_revision

    logger.info(f"[tools] GET {tool_name} | user={_mask(user_id)} | ctx={context}")
    result = await _invoke_tool(tool_name, {}, user_id, context)
    return {"result": result}
