"""
Access grants — the one place user promotion happens.

RBAC state was fragmented across four stores (AUTHORIZED_USERS env,
RapidPro groups, persona allowed_urns, ADMIN_PHONE) with no operation
that updated them coherently: `admin add` put someone in the Admins
group whose menus then 403'd at the tools API. This module gives every
enforcement point one extra source to consult — a grants file the
`access` macro edits from chat — so promotion is atomic and instant
(mtime-cached reads, no service restart).

Tiers (cumulative):
  member  — may talk to the LLM (general persona; silence lifted)
  staff   — + CRM lookup macros (RapidPro group "Staff", ROLES matrix)
  teacher — + CRM ops macros (group "Teachers")
  admin   — + talkprep/social/upload toolsets and group "Admins"
            (sensitive triggers + full RiveBot whitelist)

Deliberately NOT grantable here: the operator-only toolsets (terminal,
code_execution, config, system, email, siyuan, file, organized) and the
KG (ADMIN_PHONE-gated in-handler). Those stay with the operator.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from loguru import logger

GRANTS_PATH = os.getenv(
    "ACCESS_GRANTS_PATH", "/opt/iiab/ai-gateway/data/access_grants.json"
)

TIERS = ("member", "staff", "teacher", "admin")

# Extra toolsets each tier may reach through the tools API (union with
# persona grants and the general fallback).
# `member` must stay EMPTY: adapters/tools.py resolves toolsets as
# `granted or fallback`, so giving the member tier any toolset would drop those
# users off the DEFAULT_PERSONA fallback and take away the tools they have today.
# Member-reachable toolsets belong on the `general` persona in seed.py instead —
# that is where `contacts` (team contact sync) is granted.
TIER_TOOLSETS = {
    "member": set(),
    "staff": {"rapidpro", "contacts"},
    "teacher": {"rapidpro", "contacts"},
    "admin": {"rapidpro", "talkprep", "upload", "social", "forms", "contacts"},
}

# RapidPro group per tier (RiveBot ROLES matrix + trigger gating).
TIER_GROUP = {"staff": "Staff", "teacher": "Teachers", "admin": "Admins"}
_ALL_TIER_GROUPS = ["Staff", "Teachers", "Admins"]

_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": {}}


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", str(raw).split(":")[-1])


def _key(raw: str) -> str:
    """Grants-file key for a URN/phone/email: bare digits for phones,
    lowercased address for emails (mailto: users are grantable too)."""
    tail = str(raw).split(":")[-1].strip()
    if "@" in tail:
        return tail.lower()
    return re.sub(r"\D", "", tail)


def _display(key: str) -> str:
    return key if "@" in key else f"+{key}"


def _load() -> dict:
    """Grants keyed by bare digits. mtime-cached so checks are ~free."""
    with _lock:
        try:
            mtime = os.path.getmtime(GRANTS_PATH)
        except OSError:
            _cache.update(mtime=None, data={})
            return {}
        if _cache["mtime"] == mtime:
            return _cache["data"]
        try:
            data = json.load(open(GRANTS_PATH, encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Access grants unreadable ({e}) — treating as empty")
            data = {}
        _cache.update(mtime=mtime, data=data)
        return data


def _save(data: dict) -> None:
    with _lock:
        tmp = GRANTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, GRANTS_PATH)
        _cache.update(mtime=os.path.getmtime(GRANTS_PATH), data=data)


def is_granted(user_id: str) -> bool:
    """Any tier lifts the stranger silence (chat gate consults this)."""
    return _key(user_id) in _load()


def tier_toolsets(user_id: str) -> set:
    """Extra toolsets the caller's tier grants (tools API consults this)."""
    entry = _load().get(_key(user_id))
    if not entry:
        return set()
    return set(TIER_TOOLSETS.get(entry.get("tier", "member"), set()))


# ── RapidPro group sync ──────────────────────────────────────────────────────

def _sync_groups(phone_digits: str, tier: str, name: str) -> str:
    """Put the contact in exactly the tier's group (out of the tier groups).

    Get-or-creates the contact by whatsapp URN so promotion works before
    the person has ever messaged in.
    """
    from app.graph.tools.rapidpro import _rp_api as rp

    urn = f"whatsapp:{phone_digits}"
    found = rp("GET", "contacts.json", params={"urn": urn})
    results = found.get("results", [])
    if results:
        contact = results[0]
    else:
        # RapidPro rejects a blank name ("This field may not be blank") —
        # omit the key entirely when we don't have one.
        payload = {"urns": [urn]}
        if name:
            payload["name"] = name
        created = rp("POST", "contacts.json", json=payload)
        if created.get("error"):
            return f"⚠️ Kontak pa t ka kreye: {created['error']}"
        contact = created

    groups = rp("GET", "groups.json").get("results", [])
    by_name = {g["name"]: g["uuid"] for g in groups}
    want = TIER_GROUP.get(tier)
    if want and want not in by_name:
        made = rp("POST", "groups.json", json={"name": want})
        if made.get("uuid"):
            by_name[want] = made["uuid"]

    keep = [g["uuid"] for g in contact.get("groups", [])
            if g.get("name") not in _ALL_TIER_GROUPS]
    if want and want in by_name:
        keep.append(by_name[want])
    result = rp("POST", f"contacts.json?uuid={contact['uuid']}",
                json={"groups": keep})
    if result.get("error"):
        return f"⚠️ Gwoup pa t ka mete ajou: {result['error']}"
    return ""


def _caller(args: dict) -> str:
    """Attribute the acting operator. The tools API injects user_id into
    args; the Hermes LLM tool path doesn't — fall back to the engine's
    _current_urn contextvar so grants stay attributable."""
    caller = args.get("user_id", "")
    if not caller:
        try:
            from app.hermes.tools import _current_urn
            caller = _current_urn.get() or ""
        except Exception:
            caller = ""
    return caller


def _invalidate_caches(grant_key: str) -> None:
    """Best-effort: make grant/revoke bite now instead of after cache TTLs
    (gateway toolset cache 60s, RiveBot macro-permission cache 5 min)."""
    try:
        from app.api.adapters.tools import _TOOLSET_CACHE
        for k in [k for k in _TOOLSET_CACHE if _key(k) == grant_key]:
            _TOOLSET_CACHE.pop(k, None)
    except Exception as e:
        logger.warning(f"Toolset cache invalidation failed for {_display(grant_key)}: {e}")
    try:
        import httpx
        rivebot = os.getenv("RIVEBOT_URL", "http://127.0.0.1:8087")
        httpx.post(f"{rivebot}/flush-auth-cache", timeout=2.0)
    except Exception as e:
        logger.warning(f"RiveBot cache invalidation failed for {_display(grant_key)}: {e}")


# ── The chat macro ───────────────────────────────────────────────────────────

def macro_access(args: dict, **kw) -> str:
    """access grant <phone> <tier> | access revoke <phone> | access list"""
    # Direct calls pass _args; the tools API maps positional args into the
    # single "action" schema field as one joined string — accept both.
    action_args = args.get("_args") or str(args.get("action", "")).split()
    if not action_args:
        return ("⚠️ Itilizasyon: `access grant <nimewo> <member|staff|teacher|admin>`, "
                "`access revoke <nimewo>`, `access list`")

    action = action_args[0].lower()
    grants = dict(_load())

    if action == "list":
        if not grants:
            return "📭 Okenn moun pa gen aksè eksplisit — sèlman operatè a."
        lines = ["🛡️ *Aksè yo:*"]
        for k, e in sorted(grants.items()):
            lines.append(f"• {_display(k)} — *{e.get('tier','?')}* "
                         f"({e.get('name','') or 'san non'}, {e.get('at','')[:10]})")
        return "\n".join(lines)

    if action == "grant":
        if len(action_args) < 3:
            return "⚠️ Itilizasyon: `access grant <nimewo> <member|staff|teacher|admin>` [non]"
        key, tier = _key(action_args[1]), action_args[2].lower()
        is_email = "@" in key
        name = " ".join(action_args[3:]) if len(action_args) > 3 else ""
        if not key or (not is_email and len(key) < 8):
            return f"⚠️ Nimewo pa valid: {action_args[1]}"
        if tier not in TIERS:
            return f"⚠️ Nivo pa valid: {tier}. Chwazi: {', '.join(TIERS)}"
        grants[key] = {
            "tier": tier, "name": name,
            "granted_by": _caller(args),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save(grants)
        # Sync groups for EVERY tier — member has no tier group, but the sync
        # still strips stale Staff/Teachers/Admins membership on a demotion.
        # Email grants have no WhatsApp URN, so there is no group to sync.
        group_err = "" if is_email else _sync_groups(key, tier, name)
        _invalidate_caches(key)
        logger.info(f"Access GRANT {_display(key)} tier={tier} by {_caller(args)}")
        extra = f"\n{group_err}" if group_err else ""
        return (f"✅ {_display(key)} gen nivo *{tier}* kounye a "
                f"(chat: wi{', gwoup: ' + TIER_GROUP[tier] if tier in TIER_GROUP and not is_email else ''})."
                f"{extra}")

    if action == "revoke":
        if len(action_args) < 2:
            return "⚠️ Itilizasyon: `access revoke <nimewo>`"
        key = _key(action_args[1])
        if key not in grants:
            return f"📭 {_display(key)} pa t gen okenn aksè."
        name = grants[key].get("name", "")
        del grants[key]
        _save(grants)
        # member = no tier group; email grants never had a group
        group_err = "" if "@" in key else _sync_groups(key, "member", name)
        _invalidate_caches(key)
        logger.info(f"Access REVOKE {_display(key)} by {_caller(args)}")
        return f"🗑️ Aksè {_display(key)} retire (li tounen etranje — silans)." + (
            f"\n{group_err}" if group_err else "")

    return f"❌ Aksyon enkoni: `{action}`. Sèvi ak: grant, revoke, list."
