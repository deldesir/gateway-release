"""System operations tools (ADR-011 migration).

Layer 2 macros for system operations. Follows organized.py / talkprep.py
pattern: macro_* functions exposed via RiveBot _common/system.rive triggers
and macro_bridge.

Auth tiers:
  T3 (user-self): macro_reset, macro_debug, macro_noai, macro_enableai
  T2 (admin):     macro_noai_global, macro_enableai_global, macro_noai_status,
                  macro_reload, macro_health, macro_skills, macro_flow
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_sessions_dir = Path(os.getenv(
    "HERMES_HOME", str(Path.home() / ".hermes")
)) / "sessions"

_skills_dir = Path(os.getenv(
    "HERMES_HOME", str(Path.home() / ".hermes")
)) / "skills"

RIVEBOT_URL = os.getenv("RIVEBOT_URL", "http://127.0.0.1:8087")


# ── User-Self (T3, no auth needed) ──────────────────────────────────────────

from app.hermes.engine import get_session_id

def macro_reset(args: dict, **kw) -> str:
    """Reset the current user's conversation session.

    Deletes the Hermes session file for this user's thread.

    Returns:
        Confirmation of session reset.
    """
    user_id = args.get("user_id", "")
    persona = args.get("persona", "assistant")
    session_id = get_session_id(user_id, persona)

    session_file = _sessions_dir / f"session_{session_id}.json"

    deleted = False
    if session_file.exists():
        session_file.unlink()
        deleted = True

    if deleted:
        return "✅ Memory wiped. Conversation history has been reset."
    return "✅ No previous conversation found. Starting fresh."


def macro_debug(args: dict, **kw) -> str:
    """Return system diagnostics for the current user.

    Returns:
        User ID, thread, and persona info.
    """
    user_id = args.get("user_id", "")
    return (
        f"🐛 *System Diagnostics*\n"
        f"• User: `{user_id}`\n"
        f"• Sessions dir: `{_sessions_dir}`\n"
        f"• RiveBot: `{RIVEBOT_URL}`"
    )


def macro_noai(args: dict, **kw) -> str:
    """Disable AI for the calling user only.

    Deterministic RiveBot triggers keep working; only Hermes AI fallback
    is suppressed.

    Returns:
        Confirmation of AI disable for this user.
    """
    user_id = args.get("user_id", "")
    persona = args.get("persona", "*")

    try:
        httpx.post(
            f"{RIVEBOT_URL}/set-var",
            json={"persona": persona, "user": user_id, "var": "noai", "value": "true"},
            timeout=2.0
        )
    except Exception as e:
        return f"❌ Could not reach RiveBot: {e}"
    logger.info(f"[noai] Disabled for {user_id} on {persona}")
    return (
        "🔇 AI disabled for you.\n"
        "Type *enableai* to re-enable."
    )


def macro_enableai(args: dict, **kw) -> str:
    """Re-enable AI for the calling user.

    Returns:
        Confirmation of AI re-enable.
    """
    user_id = args.get("user_id", "")
    persona = args.get("persona", "*")

    try:
        httpx.post(
            f"{RIVEBOT_URL}/set-var",
            json={"persona": persona, "user": user_id, "var": "noai", "value": "false"},
            timeout=2.0
        )
    except Exception as e:
        return f"❌ Could not reach RiveBot: {e}"
    logger.info(f"[noai] Re-enabled for {user_id} on {persona}")
    return "✅ AI re-enabled for you."


# ── Admin (T2, RBAC-gated via ADMIN_MACROS) ─────────────────────────────────

def macro_noai_global(args: dict, **kw) -> str:
    """Disable AI for ALL users globally.

    Returns:
        Confirmation of global AI disable.
    """
    user_id = args.get("user_id", "")

    try:
        httpx.post(
            f"{RIVEBOT_URL}/set-var",
            json={"persona": "*", "user": "*", "var": "noai", "value": "true"},
            timeout=2.0
        )
    except Exception as e:
        return f"❌ Could not reach RiveBot: {e}"
    logger.warning(f"[noai] GLOBAL disable by {user_id}")
    return (
        "🔇 AI disabled for all users.\n"
        "Type *enableai all* to re-enable."
    )


def macro_enableai_global(args: dict, **kw) -> str:
    """Re-enable AI for ALL users globally.

    Returns:
        Confirmation of global AI re-enable.
    """
    user_id = args.get("user_id", "")

    try:
        httpx.post(
            f"{RIVEBOT_URL}/set-var",
            json={"persona": "*", "user": "*", "var": "noai", "value": "false"},
            timeout=2.0
        )
    except Exception as e:
        return f"❌ Could not reach RiveBot: {e}"
    logger.info(f"[noai] GLOBAL re-enable by {user_id}")
    return "✅ AI re-enabled for all users."


def macro_noai_status(args: dict, **kw) -> str:
    """Show current noai state (global + per-user).

    Returns:
        NoAI status summary.
    """
    try:
        import httpx as _httpx
        resp = _httpx.get(f"{RIVEBOT_URL}/noai-status", timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            global_flag = "🔴 ON" if data.get("global") else "🟢 OFF"
            lines = [f"🔍 *NoAI Status*\n\nGlobal: {global_flag}"]
            users = data.get("users", {})
            if users:
                for persona, uids in users.items():
                    lines.append(f"\n*{persona}*: {', '.join(uids)}")
            else:
                lines.append("\nNo individual users have noai set.")
            return "\n".join(lines)
        return f"❌ RiveBot returned {resp.status_code}"
    except Exception as e:
        return f"❌ Could not reach RiveBot: {e}"


def macro_reload(args: dict, **kw) -> str:
    """Reload all RiveBot brain files.

    Sends a POST to the RiveBot /reload endpoint and reports which
    persona engines were reloaded.

    Returns:
        Reload confirmation with list of reloaded engines.
    """
    try:
        resp = httpx.post(f"{RIVEBOT_URL}/reload", timeout=10)
        # Also flush auth cache so RBAC changes take effect immediately
        httpx.post(f"{RIVEBOT_URL}/flush-auth-cache", timeout=2.0)
        data = resp.json()
        reloaded = data.get("reloaded", [])
        return (
            f"✅ RiveBot brains reloaded!\n"
            f"• Engines: {', '.join(reloaded) if reloaded else 'none'}\n"
            f"• Count: {len(reloaded)}"
        )
    except Exception as e:
        return f"❌ Reload failed: {e}"


def macro_health(args: dict, **kw) -> str:
    """Check health of ecosystem services.

    Probes RiveBot, AI Gateway, and optional SiYuan endpoints.

    Returns:
        Health status for each service.
    """
    services = {
        "RiveBot": f"{RIVEBOT_URL}/health",
        "AI Gateway": "http://localhost:8086/health",
    }

    lines = ["🏥 *System Health Check*\n"]
    all_ok = True

    for name, url in services.items():
        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code == 200:
                lines.append(f"✅ *{name}*: OK")
            else:
                lines.append(f"⚠️ *{name}*: HTTP {resp.status_code}")
                all_ok = False
        except Exception as e:
            lines.append(f"❌ *{name}*: {e}")
            all_ok = False

    # SiYuan: verify auth, not just liveness (a 200 can hide broken auth).
    if os.getenv("SIYUAN_API_URL"):
        from app.hooks.siyuan_tools import siyuan_health
        sy = siyuan_health()
        if sy["auth_ok"]:
            pre = " (pre-release)" if sy["prerelease"] else ""
            lines.append(
                f"✅ *SiYuan*: auth OK, kernel {sy['kernel_version']}{pre}, "
                f"{sy['notebooks']} notebooks"
            )
        else:
            lines.append(f"❌ *SiYuan*: {sy['error']}")
            all_ok = False

    if all_ok:
        lines.append("\n🟢 All services healthy.")
    else:
        lines.append("\n🔴 Some services need attention.")

    return "\n".join(lines)


def macro_skills(args: dict, **kw) -> str:
    """List or delete Hermes agent-created skills.

    Actions:
        list (default): Show all agent-created skill directories.
        delete <name>: Remove a skill by name.

    Returns:
        Skill listing or deletion confirmation.
    """
    # Parse action from _args
    action_args = args.get("_args", [])
    if not action_args:
        action = args.get("action", "list")
        skill_name = args.get("skill_name", "")
    else:
        action = action_args[0] if action_args else "list"
        skill_name = action_args[1] if len(action_args) > 1 else ""

    if action == "list" or not action:
        if not _skills_dir.exists():
            return "🛡️ *Skill Audit*\nNo agent-created skills found."

        skills = [d for d in _skills_dir.iterdir() if d.is_dir() and d.name != ".cache"]
        if not skills:
            return "🛡️ *Skill Audit*\nNo agent-created skills found."

        lines = ["🛡️ *Skill Audit (Agent-Created Skills)*"]
        for s in skills:
            stat = s.stat()
            created = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"• *{s.name}* (Modified: {created})")
        lines.append("\n*To delete: `skills delete <name>`*")
        return "\n".join(lines)

    elif action == "delete" and skill_name:
        target = _skills_dir / skill_name
        # Security: path traversal protection
        if not target.exists() or target.resolve().parent != _skills_dir.resolve():
            return f"❌ Skill '{skill_name}' not found."
        try:
            shutil.rmtree(target)
            return f"🗑️ Skill '{skill_name}' successfully deleted."
        except Exception as e:
            return f"❌ Failed to delete skill: {str(e)}"

    return "❌ Usage: `skills` or `skills delete <name>`"


def macro_flow(args: dict, **kw) -> str:
    """List flows, or start/stop a RapidPro flow for the calling user.

    Actions:
        list: List all active (non-archived) flows, formatted.
        start <uuid>: Start a flow by UUID for the calling user.
        stop: Stop all active flows for the calling user.

    Returns:
        Confirmation of flow start/stop.
    """
    from temba_client.v2 import TembaClient

    # Parse action from _args
    action_args = args.get("_args", [])
    if not action_args:
        action = args.get("action", "")
        flow_uuid = args.get("flow_uuid", "")
    else:
        action = action_args[0] if action_args else ""
        flow_uuid = action_args[1] if len(action_args) > 1 else ""

    if not action:
        return "⚠️ Usage: `flow list`, `flow start <uuid>`, or `flow stop`"

    user_id = args.get("user_id", "")
    rp_host = os.getenv("RAPIDPRO_HOST")
    rp_token = os.getenv("RAPIDPRO_API_TOKEN")
    if not rp_host or not rp_token:
        return "❌ RapidPro not configured."

    client = TembaClient(rp_host, rp_token)

    if action == "list":
        # Formatted server-side so flow menus don't have to template a
        # fixed number of @webhook.json.results.N entries.
        flows = [f for f in client.get_flows().all() if not f.archived]
        if not flows:
            return "📭 No flows."
        lines = ["🔄 *Flows:*"]
        for f in sorted(flows, key=lambda f: f.name.lower()):
            lines.append(f"• {f.name}")
        return "\n".join(lines)

    urn = user_id if ":" in user_id else f"tel:{user_id}"
    contact = client.get_contacts(urn=urn).first()
    if not contact:
        return "❌ User not found in RapidPro."

    if action == "start":
        if not flow_uuid:
            return "⚠️ Usage: `flow start <uuid>`"
        client.create_flow_start(flow=flow_uuid, contacts=[contact.uuid])
        return f"🚀 Flow `{flow_uuid}` started."

    elif action == "stop":
        client.bulk_interrupt_contacts(contacts=[contact.uuid])
        return "🛑 Stopped all active flows."

    return f"❌ Unknown action: {action}"
