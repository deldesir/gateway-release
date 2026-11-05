"""
📄 Weekly sitrep draft — Friday ~16:00 (America/Port-au-Prince).

Assembles the week's work captures into a DRAFT situation report in SiYuan,
then pings the operator that a draft is ready to review + send. Seeds it from
everything logged this week: the activity notes, the tickets/incidents
resolved (close events → the items they retired), and the tickets/incidents
still open — split into this-week vs. carried-over from before. The operator
edits and sends — the job never authors the whole thing, only seeds it.

Deliverable = the draft doc (💼 Work → 📄 Sitrep → this ISO week, or Life
as a fallback). The WhatsApp/email nudge is best-effort. Run-once-per ISO
week (SITREP_FORCE=1 regenerates). Loud exit only if the draft can't be
written; a failed nudge is a warning.

Deployment-specific labels come from the environment (set them in the
gitignored .env.local, so this code stays generic):
  WORK_SITE          your city/site — appears in the From: line (default "")
  SITREP_RECIPIENT   who the sitrep is addressed to
                     (default "your supervisor / HQ")
  SITREP_LANG        draft language: en (default) or fr — localizes the
                     headings/labels only, not the captured content

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.weekly_sitrep
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta

from loguru import logger

LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
TRAVAY_NOTEBOOK = os.getenv("TRAVAY_NOTEBOOK", "💼 Work")
WORK_SITE = os.getenv("WORK_SITE", "").strip()
SITREP_RECIPIENT = os.getenv("SITREP_RECIPIENT", "your supervisor / HQ").strip()
STATE_FILE = os.getenv(
    "WEEKLY_SITREP_STATE_FILE",
    "/opt/iiab/ai-gateway/data/weekly_sitrep_state.json",
)

# Draft language. Default English (the generic committed default); a deployment
# whose sitrep goes to a French-speaking HQ sets SITREP_LANG=fr in .env.local.
# Only the scaffolding (headings/labels) is localized — captured content stays
# in whatever language the operator logged it.
SITREP_LANG = os.getenv("SITREP_LANG", "en").strip().lower()

_LABELS = {
    "en": {
        "draft": "✍️ **DRAFT** — review, edit, and send to {rcpt}. "
                 "Delete this line when ready.",
        "title": "Sitrep — Week {week} ({start} → {end})",
        "from": "From", "to": "To", "date": "Date", "role": "ICT Supervisor",
        "s1": "1. Infrastructure & Activity",
        "s1_hint": "*(Meraki / M365 / backups / power — summarize; this "
                   "week's logged activity is below)*",
        "logged": "Logged this week",
        "s2": "2. Team",
        "s2_hint": "*(who, what role — fill in)*",
        "s3": "3. Tickets & Incidents",
        "resolved": "✅ Resolved this week",
        "open_inc": "🔴 Open incidents",
        "open_tix": "🎫 Open tickets",
        "s4": "4. Planned Actions (Next 2 Weeks)",
        "s5": "5. Support Needed",
        "footer": "*Auto-draft from this week's captures (captures.jsonl): "
                  "{n_logged} logged · {n_res} resolved · {n_open} open "
                  "({n_carry} carried over, tagged ⏳). Review, edit, send. "
                  "Next sitrep: next Friday.*",
        "nudge_subject": "Sitrep draft — {week_key}",
        "ict": "Colleagues' ICT requests",
        "nudge_body": "📄 Week {week} sitrep draft ready to review:\n{url}",
    },
    "fr": {
        "draft": "✍️ **BROUILLON** — à relire, corriger et envoyer à {rcpt}. "
                 "Supprimez cette ligne une fois prêt.",
        "title": "Sitrep — Semaine {week} ({start} → {end})",
        "from": "De", "to": "À", "date": "Date", "role": "Superviseur ICT",
        "s1": "1. Infrastructure & Activité",
        "s1_hint": "*(Meraki / M365 / sauvegardes / énergie — à résumer ; "
                   "l'activité enregistrée cette semaine est ci-dessous)*",
        "logged": "Enregistré cette semaine",
        "s2": "2. Équipe",
        "s2_hint": "*(qui, quel rôle — à compléter)*",
        "s3": "3. Tickets & Incidents",
        "resolved": "✅ Résolus cette semaine",
        "open_inc": "🔴 Incidents ouverts",
        "open_tix": "🎫 Tickets ouverts",
        "s4": "4. Actions planifiées (2 prochaines semaines)",
        "s5": "5. Soutien nécessaire",
        "footer": "*Brouillon auto-généré à partir des captures de la semaine "
                  "(captures.jsonl) : {n_logged} enregistré(s) · {n_res} "
                  "résolu(s) · {n_open} ouvert(s) ({n_carry} reporté(s), "
                  "marqués ⏳). Relire, corriger, envoyer. Prochain sitrep : "
                  "vendredi prochain.*",
        "nudge_subject": "Brouillon de sitrep — {week_key}",
        "ict": "Demandes ICT des collègues",
        "nudge_body": "📄 Brouillon du sitrep de la semaine {week} prêt à "
                      "relire :\n{url}",
    },
}
_L = _LABELS.get(SITREP_LANG, _LABELS["en"])

# Human names for the language codes we can translate into.
_LANG_NAMES = {"fr": "French"}


def _admin_urn_safe() -> str | None:
    """The operator's URN for an LLM invocation, or None if unresolvable.
    Unlike daily_synthesis._admin_urn this never exits — translation is
    best-effort and must not abort the draft."""
    admin = [a.strip() for a in os.getenv("ADMIN_PHONE", "").split(",") if a.strip()]
    if not admin:
        return None
    first = admin[0]
    if ":" in first:
        return first
    if "@" in first:
        return f"mailto:{first}"
    return f"whatsapp:{first.lstrip('+')}"


def _translate_texts(texts: list[str], lang: str) -> list[str]:
    """Translate capture lines into `lang` for an HQ-facing draft, via the
    gateway's native LLM in ONE batched call. Best-effort: any failure (no LLM,
    malformed output, count mismatch, no admin urn) returns the originals so the
    draft always renders. English is a passthrough — only non-en deployments pay
    the call. Numbers, currencies and proper/technical names are preserved."""
    lang_name = _LANG_NAMES.get(lang)
    if not lang_name or not texts:
        return texts
    urn = _admin_urn_safe()
    if not urn:
        return texts
    try:
        from app.hermes.tools import register_all_tools
        register_all_tools()
        from app.hermes.engine import _invoke_sync

        system = (
            "You are a professional translator for a humanitarian medical "
            f"organisation's ICT field report. Translate each string of the given "
            f"JSON array into {lang_name}. Keep it faithful and concise. "
            "Preserve EXACTLY, untranslated: numbers, amounts and currencies "
            "(e.g. HTG), acronyms, and product/proper names such as Starlink, "
            "M365, Meraki, Digicel, Natcom, Marmota, HMIS, VLAN, SOP, SIM. Do "
            "not add, drop, merge or reorder items. Return ONLY a JSON array of "
            "the translated strings — same length and order — with no prose and "
            "no code fence."
        )
        result = _invoke_sync(
            urn=urn, persona="synthesis", message=json.dumps(texts, ensure_ascii=False),
            system_prompt=system, allowed_tools=[], platform="email",
        )
        raw = ((result or {}).get("final_response") or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        out = json.loads(raw)
        if (isinstance(out, list) and len(out) == len(texts)
                and all(isinstance(x, str) for x in out)):
            # keep the original if a line comes back blank
            return [(x.strip() or texts[i]) for i, x in enumerate(out)]
        logger.warning("Sitrep translate: shape mismatch — keeping originals")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Sitrep translate: failed ({e}) — keeping originals")
    return texts


def _die(msg: str) -> None:
    logger.error(f"Weekly sitrep ABORTED: {msg}")
    sys.exit(1)


def _load_json(path: str) -> dict:
    try:
        return json.loads(open(path).read())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        open(STATE_FILE, "w").write(json.dumps(state))
    except OSError as e:
        logger.warning(f"Weekly sitrep: cannot persist state: {e}")


def _target_notebook() -> str:
    """Prefer 💼 Work; fall back to Life if that notebook name isn't found."""
    from app.hooks.siyuan_tools import get_notebook_id
    return TRAVAY_NOTEBOOK if get_notebook_id(TRAVAY_NOTEBOOK) else LIFE_NOTEBOOK


def _ict_block(monday: datetime) -> str:
    """Colleagues' requests this week, from RapidPro's tickets. Best effort."""
    try:
        from app.graph.tools.ict_requests import directory_drift, drift_lines, figures_lines, ticket_figures

        return "\n".join(figures_lines(ticket_figures(monday), SITREP_LANG, detail=False)
                         + drift_lines(directory_drift(), SITREP_LANG))
    except Exception as exc:
        return f"({exc})"


def _render(now: datetime, monday: datetime) -> str:
    from app.graph.tools.capture import open_items, read_captures

    # Still-open tickets/incidents CREATED this week vs. carried over from
    # before it (open_items partitions cleanly at the Monday boundary: a
    # closed item is in neither list — it surfaces under "Resolved" instead).
    week = open_items(since=monday)
    prior = open_items(until=monday)
    week_tix = [r for r in week if r.get("kind") == "ticket"]
    week_inc = [r for r in week if r.get("kind") == "incident"]
    prior_tix = [r for r in prior if r.get("kind") == "ticket"]
    prior_inc = [r for r in prior if r.get("kind") == "incident"]

    # Resolved this week: close events in-window → resolve each ref to the
    # ticket/incident it retired. A close of an unknown id resolves to nothing
    # and is simply skipped (a mistyped `close` never fabricates a line).
    id_map = {r["id"]: r for r in read_captures(kinds={"ticket", "incident"})
              if r.get("id")}
    resolved, seen = [], set()
    for c in read_captures(since=monday, kinds={"close"}):
        for ref in (c.get("refs") or []):
            if ref not in seen and ref in id_map:
                seen.add(ref)
                resolved.append(id_map[ref])

    # This week's activity log — the notes the operator captured (chronological).
    notes = read_captures(since=monday, kinds={"note"})

    # Translate the captured content into the draft language (best-effort, one
    # batched LLM call for everything shown). English → passthrough. The scaffold
    # is already localized via _L; this makes the DATA match it for HQ.
    _all_rows = notes + resolved + week_tix + week_inc + prior_tix + prior_inc
    _orig = [r.get("text", "") for r in _all_rows]
    _tmap = dict(zip(_orig, _translate_texts(_orig, SITREP_LANG)))

    def _txt(r):
        t = r.get("text", "")
        return _tmap.get(t, t)

    def _lines(rows, tag=""):
        return "\n".join(f"- {tag}{_txt(r)}" for r in rows) or "- —"

    def _newest(rows):
        return list(reversed(rows))

    def _open_block(week_rows, prior_rows):
        # this week first (newest→oldest), then carried-over items tagged ⏳
        body = _lines(_newest(week_rows)) if week_rows else ""
        if prior_rows:
            carried = _lines(_newest(prior_rows), tag="⏳ ")
            body = f"{body}\n{carried}" if body else carried
        return body or "- —"

    iso = now.isocalendar()
    frm = _L["role"] + (f", {WORK_SITE}" if WORK_SITE else "")
    n_open_tix = len(week_tix) + len(prior_tix)
    n_open_inc = len(week_inc) + len(prior_inc)
    n_carry = len(prior_tix) + len(prior_inc)

    draft = _L["draft"].format(rcpt=SITREP_RECIPIENT)
    title = _L["title"].format(
        week=iso.week, start=f"{monday:%Y-%m-%d}", end=f"{now:%Y-%m-%d}")
    footer = _L["footer"].format(
        n_logged=len(notes), n_res=len(resolved),
        n_open=n_open_tix + n_open_inc, n_carry=n_carry)

    return f"""> {draft}

# {title}

**{_L['from']}:** {frm}
**{_L['to']}:** {SITREP_RECIPIENT}
**{_L['date']}:** {now:%Y-%m-%d}

## {_L['s1']}
{_L['s1_hint']}

**{_L['logged']} ({len(notes)}):**
{_lines(notes)}

## {_L['s2']}
{_L['s2_hint']}

## {_L['s3']}

**{_L['resolved']} ({len(resolved)}):**
{_lines(_newest(resolved))}

**{_L['open_inc']} ({n_open_inc}):**
{_open_block(week_inc, prior_inc)}

**{_L['open_tix']} ({n_open_tix}):**
{_open_block(week_tix, prior_tix)}

**{_L['ict']}:**
{_ict_block(monday)}

## {_L['s4']}
1. …
2. …

## {_L['s5']}
- …

---
{footer}
"""


def main() -> None:
    now = datetime.now()
    iso = now.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    force = os.getenv("SITREP_FORCE", "") == "1"

    state = _load_json(STATE_FILE)
    if state.get("week") == week_key and not force:
        logger.info(f"Weekly sitrep: {week_key} already drafted — skipping "
                    "(SITREP_FORCE=1 to regenerate)")
        return

    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    from app.hooks.siyuan_tools import (
        _siyuan_request, get_notebook_id, siyuan_doc_url, siyuan_upsert_page,
    )

    notebook = _target_notebook()
    path = f"/📄 Sitrep/{week_key}"
    # Idempotency: did the draft already exist? upsert is safe either way, but
    # the nudge must fire only on first creation (a lost state file re-running
    # in the same week must not re-nudge).
    nb_id = get_notebook_id(notebook)
    existed = bool(nb_id and (_siyuan_request(
        "filetree/getIDsByHPath", {"notebook": nb_id, "path": path}, quiet=True
    ).get("data") or []))
    block_id = siyuan_upsert_page(notebook, path, _render(now, monday))
    if not block_id:
        _die(f"could not write sitrep draft to {notebook}{path}")

    url = siyuan_doc_url(block_id)
    _save_state({"week": week_key, "doc_id": block_id, "url": url})
    logger.info(f"Weekly sitrep OK: {notebook}{path} → {block_id} "
                f"({'refreshed' if existed else 'created'})")

    if existed:
        logger.info("Weekly sitrep: draft already existed — no re-nudge (idempotent)")
        return

    from app.graph.tools.settings import is_snoozed
    if is_snoozed("sitrep", now):
        logger.info("Weekly sitrep: nudge snoozed — draft written, no ping")
        return

    # best-effort nudge — the draft is already safe on disk
    try:
        from app.graph.tools.email import notify_admin
        msg = _L["nudge_body"].format(week=iso.week, url=url)
        if notify_admin(_L["nudge_subject"].format(week_key=week_key), msg):
            logger.info("Weekly sitrep: nudge sent")
        else:
            logger.warning("Weekly sitrep: nudge not delivered")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Weekly sitrep: nudge error (draft still written): {e}")


if __name__ == "__main__":
    main()
