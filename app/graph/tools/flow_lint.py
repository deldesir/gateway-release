"""Flow lint: a tapped row is a message.

Mailroom matches the first word of every incoming message against the workspace's keyword
triggers *before* resuming a waiting flow, unless that flow ignores triggers. So a list row
or button whose title starts with a keyword hijacks the dialog it belongs to (found the hard
way: "Lang repons yo" against `lang`, "AI Grading" against `ai`). This module checks, for
every flow of every workspace:

1. no static row (base language or localised) in a flow that honours triggers starts with
   one of that workspace's keywords;
2. a closed dialog (a flow that posts to a gateway ``*_dialog`` tool) ignores triggers;
3. no row title a dialog tool can send starts with a keyword of a workspace whose flows
   call that tool.

Section labels and row descriptions are never sent as text and are not checked. ``check``
is pure (flows and triggers as plain dicts) and is what the test suite runs against the
tools' own menus; ``live`` reads the shared ``temba`` database and is what the morning
Radar reports.
"""

import json
import os
import re
import unicodedata

from loguru import logger

# The keyword triggers as recorded on 2026-11-03, per workspace, for the test suite; the
# live check reads the real ones. Add here when a trigger is added.
KNOWN_KEYWORDS = {
    1: {"support", "sales", "ops", "prep", "train", "ai", "alva", "lang", "reglaj", "settings", "paramet", "paramèt"},
    3: {"sync", "kontak", "contacts", "menu", "meni", "reglaj", "settings", "paramet", "paramèt",
        "boutik", "shop", "magasin", "magazen", "ai", "alva", "pale", "prep"},
}
# Which workspaces' flows call each dialog tool (the live check derives this from the flows).
KNOWN_DIALOG_ORGS = {"settings_dialog": {1, 3}, "contact_sync_dialog": {3}, "retail_dialog": {3}}


def norm(text) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


def first_word(title: str) -> str:
    title = (title or "").split("|")[-1].split("<extra>")[0].strip()
    return norm(title.split()[0]) if title.split() else ""


def static_rows(definition: dict) -> list[str]:
    """Every literal row of a flow's send actions, base and localised; expressions skipped."""
    rows = []
    for node in definition.get("nodes", []):
        for action in node.get("actions", []):
            if action.get("type") == "send_msg":
                rows += [q for q in action.get("quick_replies", []) if not str(q).startswith("@")]
    for items in definition.get("localization", {}).values():
        for fields in items.values():
            rows += [q for q in fields.get("quick_replies", []) if not str(q).startswith("@")]
    return rows


def dialog_tools_called(definition: dict) -> set[str]:
    out = set()
    for node in definition.get("nodes", []):
        for action in node.get("actions", []):
            if action.get("type") == "call_webhook":
                m = re.search(r"/v1/tools/([a-z0-9_]+_dialog)\b", str(action.get("url", "")))
                if m:
                    out.add(m.group(1))
    return out


def dynamic_titles(tool: str) -> list[str]:
    """Every row title the dialog tool can send, in every language it speaks."""
    if tool == "settings_dialog":
        from app.graph.tools import settings
        titles = [title for _s, _k, title in settings.DIALOG] + ["Soti", "Retounen"]
        for _s, key, _t in settings.DIALOG:
            titles += [settings._label(c) for c in settings.SCHEMA[key]["choices"]]
        return titles
    if tool == "contact_sync_dialog":
        from app.graph.tools import contact_sync as C
        titles = []
        for lang in ("ht", "en", "fr"):
            for spec in (C.MAIN_MENU, C.ICT_MENU):
                titles += [o["title"] for o in C.menu_options(spec, lang)]
            titles += [o["title"] for o in C._dept_options(lang)]
            titles += [o["title"] for o in C._lang_options(lang)]
            titles += [C._t("back", lang), C._t("menu_button", lang)]
        return titles
    return []


def reaches_gateway(definition: dict) -> bool:
    """A flow that calls the model or a gateway tool: the only flows a stranger must never reach."""
    for node in definition.get("nodes") or []:
        for a in node.get("actions") or []:
            if a.get("type") == "call_llm":
                return True
            if a.get("type") == "call_webhook" and "/v1/tools/" in str(a.get("url") or ""):
                return True
    return False


# Flows a person the house does not know may reach, by that line's own decision: the directory
# line tells a colleague they are not on any list, and the tool behind it authorises by the
# directory's grants. Recorded exceptions (D-014); anything else must be a word for a group.
OPEN_FLOWS = {(3, "Directory sync"), (3, "ICT request closed")}


def doors(flows: list[dict], triggers: list[dict]) -> list[str]:
    """D-014: a trigger that reaches a gateway flow is restricted to a group of known numbers,
    and no catch-all reaches one. Only the triggers a person's message can fire count (keyword
    and catch-all); triggers carry trigger_type, flow_id and groups."""
    gateway_flows = {}
    for f in flows:
        definition = f.get("definition") or {}
        if isinstance(definition, str):
            definition = json.loads(definition)
        if reaches_gateway(definition) and (int(f["org_id"]), f["name"]) not in OPEN_FLOWS:
            gateway_flows[(int(f["org_id"]), int(f["id"]))] = f["name"]
    findings = []
    for t in triggers:
        if t.get("trigger_type") not in ("K", "C"):
            continue
        key = (int(t["org_id"]), int(t.get("flow_id") or 0))
        if key not in gateway_flows:
            continue
        name = gateway_flows[key]
        if t.get("trigger_type") == "C":
            findings.append(f"workspace {key[0]}: a catch-all trigger reaches the gateway flow \"{name}\" (flow {key[1]}): "
                            f"every message of every number travels; it must be a word for a group (D-014)")
        elif not (t.get("groups") or []):
            findings.append(f"workspace {key[0]}: trigger {t.get('id', '?')} reaches the gateway flow \"{name}\" (flow {key[1]}) "
                            f"with no group: a stranger's word would travel (D-014)")
    return findings


def check(flows: list[dict], triggers: list[dict]) -> list[str]:
    """Findings as plain sentences. flows: {org_id, id, name, ignore_triggers, definition};
    triggers: {org_id, keywords} and, when known, {trigger_type, flow_id, groups, id}."""
    keywords: dict[int, set[str]] = {}
    for t in triggers:
        keywords.setdefault(int(t["org_id"]), set()).update(norm(k) for k in (t.get("keywords") or []))
    findings = []
    dialog_orgs: dict[str, set[int]] = {}
    for f in flows:
        org = int(f["org_id"])
        kws = keywords.get(org, set())
        definition = f.get("definition") or {}
        if isinstance(definition, str):
            definition = json.loads(definition)
        tools = dialog_tools_called(definition)
        if f.get("ignore_triggers"):
            continue                      # nothing sent inside it can be hijacked
        if tools:
            findings.append(f"workspace {org}: flow {f['id']} \"{f['name']}\" is a dialog "
                            f"({', '.join(sorted(tools))}) but does not ignore keyword triggers")
        for tool in tools:
            dialog_orgs.setdefault(tool, set()).add(org)
        for row in static_rows(definition):
            w = first_word(row)
            if w and w in kws:
                findings.append(f"workspace {org}: flow {f['id']} \"{f['name']}\" row \"{row}\" "
                                f"starts with the keyword \"{w}\"")
    for tool, orgs in dialog_orgs.items():
        for title in dynamic_titles(tool):
            w = first_word(title)
            for org in sorted(orgs):
                if w and w in keywords.get(org, set()):
                    findings.append(f"workspace {org}: {tool} can send the row \"{title}\", "
                                    f"which starts with the keyword \"{w}\"")
    findings += doors(flows, triggers)
    return sorted(set(findings))


def _db_uri() -> str:
    return os.getenv("POSTGRES_URI", "").replace("+asyncpg", "").replace("+psycopg", "")


def live() -> list[str]:
    """The findings against the real flows and triggers of every workspace in the shared
    database; a connection problem is one finding, never a crash."""
    uri = _db_uri()
    if not uri.startswith("postgresql"):
        return ["flow lint: no PostgreSQL URI configured"]
    try:
        import psycopg
        with psycopg.connect(uri, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "select t.id, t.org_id, t.trigger_type, t.flow_id, t.keywords, "
                "coalesce(array_agg(g.name) filter (where g.name is not null), '{}') from triggers_trigger t "
                "left join triggers_trigger_groups tg on tg.trigger_id = t.id "
                "left join contacts_contactgroup g on g.id = tg.contactgroup_id "
                "where t.is_active and not t.is_archived group by t.id")
            triggers = [{"id": i, "org_id": o, "trigger_type": tt, "flow_id": fid, "keywords": (k or []) if tt == "K" else [], "groups": list(gs or [])}
                        for i, o, tt, fid, k, gs in cur.fetchall()]
            cur.execute(
                "select f.org_id, f.id, f.name, f.ignore_triggers, r.definition from flows_flow f "
                "join lateral (select definition from flows_flowrevision where flow_id = f.id "
                "order by revision desc limit 1) r on true "
                "where f.is_active and not f.is_archived and f.name not like 'Sample Flow%'")
            flows = [{"org_id": o, "id": i, "name": n, "ignore_triggers": ig, "definition": d}
                     for o, i, n, ig, d in cur.fetchall()]
    except Exception as e:
        logger.warning(f"flow lint: database unavailable: {e}")
        return [f"flow lint: could not read the flows ({type(e).__name__})"]
    return check(flows, triggers) + keyword_drift(triggers)


def keyword_drift(triggers: list[dict]) -> list[str]:
    """The recorded keyword list (what the test suite checks) against the live one: a
    keyword added or removed in RapidPro must land in KNOWN_KEYWORDS, or the tests go stale."""
    live_kws: dict[int, set[str]] = {}
    for t in triggers:
        live_kws.setdefault(int(t["org_id"]), set()).update(norm(k) for k in (t.get("keywords") or []))
    out = []
    for org in sorted(set(live_kws) | set(KNOWN_KEYWORDS)):
        known = {norm(k) for k in KNOWN_KEYWORDS.get(org, set())}
        added, removed = sorted(live_kws.get(org, set()) - known), sorted(known - live_kws.get(org, set()))
        if added or removed:
            out.append(f"workspace {org}: KNOWN_KEYWORDS in the gateway is stale"
                       + (f"; add {', '.join(added)}" if added else "")
                       + (f"; remove {', '.join(removed)}" if removed else ""))
    return out


def live_keywords() -> dict[int, set[str]] | None:
    """The live keyword lists when the database is reachable, else None (tests fall back
    to KNOWN_KEYWORDS)."""
    uri = _db_uri()
    if not uri.startswith("postgresql"):
        return None
    try:
        import psycopg
        with psycopg.connect(uri, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("select org_id, keywords from triggers_trigger "
                        "where is_active and not is_archived and trigger_type = 'K'")
            out: dict[int, set[str]] = {}
            for org, kws in cur.fetchall():
                out.setdefault(int(org), set()).update(kws or [])
            return out
    except Exception:
        return None
