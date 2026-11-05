import os
import json
import logging
from typing import Dict, Any, Optional

try:
    from temba_client.v2 import TembaClient
except ImportError:
    # Fallback for dev/test environments without the library
    TembaClient = None

logger = logging.getLogger(__name__)

def get_client() -> Optional[Any]:
    """
    Factory to get the RapidPro client with environment variables.
    """
    host = os.getenv("RAPIDPRO_HOST")
    token = os.getenv("RAPIDPRO_API_TOKEN") # Note: Check .env if this name matches
    
    if not host or not token:
        logger.warning("RapidPro host/token not configured.")
        return None
        
    if not TembaClient:
        logger.error("temba_client library not installed.")
        return None

    return TembaClient(host, token)

def fetch_dossier(args: dict, **kwargs) -> str:
    """
    Fetch the user's official profile (Name, District, Language) from RapidPro.
    Use this at the START of a conversation to populate the Dossier.
    """
    urn = args.get("urn", "")
    if not urn:
        return json.dumps({"error": "No URN provided"})

    client = get_client()
    if not client:
        return json.dumps({"error": "RapidPro client unavailable"})

    try:
        contact = client.get_contacts(urn=urn).first()
        if not contact:
            return json.dumps({"error": "Contact not found in RapidPro"})
        
        return json.dumps({
            "name": contact.name,
            "language": contact.language,
            "uuid": contact.uuid,
            "fields": contact.fields or {}
        })
    except Exception as e:
        logger.error(f"Failed to fetch dossier for {urn}: {e}")
        return json.dumps({"error": str(e)})

def start_flow(args: dict, **kwargs) -> str:
    """Trigger a specific RapidPro flow for the user."""
    from app.graph.prompts import FLOW_REGISTRY
    
    urn = args.get("urn", "")
    flow_identifier = args.get("flow_identifier", "")

    if not urn or not flow_identifier:
        return json.dumps({"error": "Missing urn or flow_identifier"})
    
    client = get_client()
    if not client:
        return json.dumps({"error": "RapidPro client unavailable"})

    # Resolve Name -> UUID
    flow_uuid = FLOW_REGISTRY.get(flow_identifier, flow_identifier)

    try:
        client.create_flow_start(flow=flow_uuid, contacts=[urn])
        return json.dumps({"status": "success", "message": f"Flow '{flow_identifier}' (UUID: {flow_uuid}) started successfully."})
    except Exception as e:
        logger.error(f"Failed to start flow {flow_uuid} for {urn}: {e}")
        return json.dumps({"error": f"Failed to start flow: {e}"})


def start_crm_ops(args: dict, **kwargs) -> str:
    """Start the CRM operations flow for the admin user (ADR-010).

    Triggered by RiveBot's admin.rive via macro_bridge. Returns {{noreply}}
    to suppress the AI Gateway response — the RapidPro flow takes over the
    WhatsApp session entirely.

    The flow itself enforces authorization via a has_group(Admins) guard node.
    """
    # macro_bridge injects the URN as user_id via X-User-Id header
    urn = args.get("user_id", "")
    if not urn:
        return json.dumps({"error": "Missing user context."})

    # Ensure URN has a scheme prefix
    if ":" not in urn:
        urn = f"whatsapp:{urn}"

    flow_uuid = os.getenv("CRM_ROUTER_FLOW_UUID") or os.getenv("CRM_OPS_FLOW_UUID")
    if not flow_uuid:
        logger.error("[crm_ops] CRM_ROUTER_FLOW_UUID not configured")
        return "⚠️ CRM operations not configured. Set CRM_ROUTER_FLOW_UUID."

    client = get_client()
    if not client:
        return json.dumps({"error": "RapidPro client unavailable"})

    try:
        client.create_flow_start(
            flow=flow_uuid,
            urns=[urn],
            exclude_active=False,  # MUST interrupt existing session (Finding 6)
        )
        logger.info(f"[crm_ops] Started CRM ops flow for {urn}")
        return "{{noreply}}"  # Suppress AI response — flow sends its own menu
    except Exception as e:
        logger.error(f"[crm_ops] Failed to start CRM ops for {urn}: {e}")
        return f"⚠️ Failed to start CRM ops: {e}"


def send_crm_help(args: dict, **kwargs) -> str:
    """Send a WhatsApp Quick Reply button explaining how to access CRM ops (ADR-010 Layer A).

    Triggered by discovery phrases in admin.rive (e.g. "how do I manage contacts").
    Sends a tappable button via WuzAPI so the admin can activate the CRM flow
    without remembering the exact trigger text.

    Falls back to plain text if WuzAPI is unavailable.
    Returns {{noreply}} so the AI Gateway suppresses its own response.
    """
    urn = args.get("user_id", "")
    # Strip scheme prefix to get raw phone digits for WuzAPI
    phone = urn.split(":")[-1].lstrip("+") if urn else ""

    buttons = [{"DisplayText": "ops menu", "Type": "quickreply"}]
    msg = "🗂️ To open CRM Operations, tap the button below or type *ops menu* at any time."
    footer = "Tap to activate · Type 'exit_ops' to leave"

    from app.api.middleware.wuzapi_token import wuzapi_token_available

    if phone and wuzapi_token_available():
        from app.api.middleware.wuzapi_client import send_buttons
        import asyncio
        import concurrent.futures

        def _run_async():
            return asyncio.run(send_buttons(phone=phone, content=msg, footer=footer, buttons=buttons))

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                success = pool.submit(_run_async).result(timeout=6.0)
            if success:
                logger.info(f"[crm_help] Button message sent to {phone}")
                return "{{noreply}}"
        except Exception as e:
            logger.warning(f"[crm_help] WuzAPI button send failed: {e}")

    # Plain text fallback (no WuzAPI or send failed)
    logger.info(f"[crm_help] Fallback plain-text for {urn}")
    return "🗂️ Type *ops menu* to access CRM Operations, or *exit_ops* to leave a session."


# ── Layer 2 Direct Commands (ADR-011 T2) ─────────────────────────────────────
# These bypass the flow system entirely. Single message in → single message out.
# Auth is enforced in macro_bridge.py before these are called.

import requests as _requests


def _rp_api(method: str, endpoint: str, **kwargs) -> dict:
    """Call RapidPro API v2. Returns parsed JSON or error dict."""
    host = os.getenv("RAPIDPRO_HOST", "http://localhost:8000")
    # `token=` selects another workspace of the same RapidPro (the contact
    # directory may live in its own); the assistant's token is the default.
    token = kwargs.get("token") or os.getenv("RAPIDPRO_API_TOKEN", "")
    url = f"{host}/api/v2/{endpoint}"
    headers = {"Authorization": f"Token {token}"}
    try:
        if method == "GET":
            r = _requests.get(url, headers=headers, params=kwargs.get("params"), timeout=8)
        else:
            headers["Content-Type"] = "application/json"
            r = _requests.post(url, headers=headers, json=kwargs.get("json"), timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"[crm_l2] {method} {endpoint} failed: {e}")
        return {"error": str(e)}


def crm_list_groups(args: dict, **kwargs) -> str:
    """Layer 2: list groups (segments) — instant single-message response.

    Usage: type 'list groups' in WhatsApp.
    """
    data = _rp_api("GET", "groups.json")
    if "error" in data:
        return f"⚠️ Failed to list segments: {data['error']}"

    results = data.get("results", [])
    if not results:
        return "📋 No segments found."

    lines = ["📋 *Segments:*\n"]
    for g in results[:15]:  # Cap at 15 to avoid WhatsApp message truncation
        name = g.get("name", "?")
        count = g.get("count", 0)
        lines.append(f"• {name} ({count} members)")

    total = data.get("count", len(results))
    if total > 15:
        lines.append(f"\n_Showing 15 of {total}. Use *ops* → Segments for full list._")

    lines.append("\n💡 Type *ops* for guided CRM operations.")
    return "\n".join(lines)


def crm_lookup_contact(args: dict, **kwargs) -> str:
    """Layer 2: lookup a contact by phone number.

    Usage: type 'lookup <phone>' in WhatsApp.
    Positional arg: phone number (digits only).
    """
    phone = args.get("phone", "").strip()
    if not phone:
        return "⚠️ Missing phone number.\n\n*Usage:* `lookup <phone number>`"

    # Normalize: strip + and leading zeros
    phone = phone.lstrip("+").lstrip("0")

    data = _rp_api("GET", "contacts.json", params={"urn": f"whatsapp:{phone}"})
    if "error" in data:
        return f"⚠️ Lookup failed: {data['error']}"

    results = data.get("results", [])
    if not results:
        return f"🔍 No contact found for *{phone}*.\n\n💡 Type *ops* → Contacts → Create to add them."

    c = results[0]
    name = c.get("name") or "_(no name)_"
    uuid = c.get("uuid", "?")[:8]
    lang = c.get("language") or "—"
    groups = [g.get("name", "?") for g in c.get("groups", [])]
    fields = c.get("fields", {})

    lines = [
        f"👤 *{name}*\n",
        f"📱 whatsapp:{phone}",
        f"🏷️ Segments: {', '.join(groups) if groups else '_(none)_'}",
        f"🌐 Language: {lang}",
        f"🔑 UUID: ...{uuid}",
    ]

    # Show non-empty custom fields
    if fields:
        visible = {k: v for k, v in fields.items() if v}
        if visible:
            lines.append("\n📝 *Fields:*")
            for k, v in list(visible.items())[:5]:
                lines.append(f"  • {k}: {v}")

    lines.append("\n💡 Type *ops* → Contacts for more operations.")
    return "\n".join(lines)


def crm_org_info(args: dict, **kwargs) -> str:
    """Layer 2: show organization info — instant single-message response.

    Usage: type 'org info' in WhatsApp.
    """
    data = _rp_api("GET", "org.json")
    if "error" in data:
        return f"⚠️ Failed to get org info: {data['error']}"

    name = data.get("name", "?")
    timezone = data.get("timezone", "?")
    languages = data.get("languages", [])
    credits = data.get("credits", {})

    lines = [
        f"🏢 *{name}*\n",
        f"🕐 Timezone: {timezone}",
        f"🌐 Languages: {', '.join(languages) if languages else '_(none)_'}",
    ]

    if credits:
        remaining = credits.get("remaining", "?")
        lines.append(f"💳 Credits: {remaining}")

    lines.append("\n💡 Type *ops* → System for more details.")
    return "\n".join(lines)


def crm_create_group(args: dict, **kwargs) -> str:
    """Layer 2: create a segment (RapidPro group).

    Usage: type 'create group <name>' in WhatsApp.
    Positional arg: group name (can be multi-word).
    """
    name = args.get("name", "").strip()
    if not name:
        return "⚠️ Missing segment name.\n\n*Usage:* `create group <name>`"

    data = _rp_api("POST", "groups.json", json={"name": name})
    if "error" in data:
        return f"⚠️ Failed to create segment: {data['error']}"

    uuid = data.get("uuid", "?")
    return (
        f"✅ Segment *{name}* created!\n\n"
        f"🔑 UUID: {uuid}\n\n"
        "💡 Add contacts via *ops* → Contacts."
    )
