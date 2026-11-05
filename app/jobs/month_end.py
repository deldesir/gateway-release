"""
🗓️ Month-end pack — 1st of the month ~08:00 (America/Port-au-Prince).

Summarizes the PREVIOUS calendar month for the three roles that have
month-boundary deliverables, then sends ONE scarce nudge:

  1. Monthly ICT report — a DRAFT (standard monthly-report headings)
     seeded from last month's ticket/incident captures → 💼 Work.
  2. Pioneer hours — last month's captured total (your own tally).
  3. Secretary — a reminder + checklist that congregation reports are due
     (the numbers themselves live in Organized; this is the nudge).

Deliverable = the report draft doc + the nudge. Run-once-per-month
(MONTHEND_FORCE=1 regenerates). Loud exit only if the draft can't be
written.

Language: MONTHEND_LANG (en default) localizes the scaffolding + month names
and translates the captured content; it falls back to SITREP_LANG so setting
one language covers both HQ-facing report drafts.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.month_end
"""

import json
import os
import sys
from datetime import datetime

from loguru import logger

# Reuse the sitrep's content translator + urn helper (same pattern as
# week_ahead importing from daily_synthesis) — no second copy to maintain.
from app.jobs.weekly_sitrep import _translate_texts  # noqa: E402

LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
TRAVAY_NOTEBOOK = os.getenv("TRAVAY_NOTEBOOK", "💼 Work")
WORK_SITE = os.getenv("WORK_SITE", "").strip()
# ministry goal + secretary nudges are tunable from chat — see settings store.
STATE_FILE = os.getenv(
    "MONTH_END_STATE_FILE", "/opt/iiab/ai-gateway/data/month_end_state.json"
)

# Report language. Falls back to SITREP_LANG (the two HQ-facing report drafts
# usually share one language) then English. Only scaffolding + month names are
# localized here; captured content is translated by _translate_texts.
MONTHEND_LANG = (os.getenv("MONTHEND_LANG") or os.getenv("SITREP_LANG")
                 or "en").strip().lower()

_MONTHS = {
    "en": ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
    "fr": ["", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
           "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"],
}

_LABELS = {
    "en": {
        "draft": "✍️ **DRAFT** — review, edit, and send. Delete this line when ready.",
        "title": "ICT Monthly Report{site} — {month} {year}",
        "author": "Author", "role": "ICT Supervisor",
        "distribution": "Distribution: HoM, LogCo, HQ (if required)",
        "s_infra": "Infrastructure Status", "fill": "*(fill in)*",
        "s_tickets": "Tickets / Requests",
        "total": "Total captured: **{tix} tickets**, **{inc} incidents**.",
        "tbl_head": "| Category | Count | Avg Resolution |",
        "tbl_sep": "|----------|-------|----------------|",
        "tbl_row": "| *(categorize)* | | |",
        "s_incidents": "Incidents", "s_actions": "Key Actions Taken",
        "s_planned": "Planned for Next Month",
        "s_kpi": "ICT KPIs — This Month",
        "kpi_hint": "*(uptime, backup restore test, tickets closed — fill in)*",
        "footer": "*Auto-draft from captures.jsonl ({n} open item(s)). "
                  "Next report: 1st of next month.*",
        "nudge_subject": "End of {month} {year}",
        "nudge_head": "🗓️ End of {month} {year}:",
        "nudge_work": "• Work: monthly report draft ready ({tix} tickets, "
                      "{inc} incidents) — {url}",
        "nudge_ministry": "• Ministry: you logged {hours}h in {month}",
        "nudge_goal": " (goal {goal:.0f}h).\n", "nudge_dot": ".\n",
        "nudge_secretary": "• Secretary: compile & submit the congregation "
                           "report for {month} (field-service tallies in Organized).",
    },
    "fr": {
        "draft": "✍️ **BROUILLON** — à relire, corriger et envoyer. "
                 "Supprimez cette ligne une fois prêt.",
        "title": "Rapport ICT mensuel{site} — {month} {year}",
        "author": "Auteur", "role": "Superviseur ICT",
        "distribution": "Distribution : HoM, LogCo, HQ (si nécessaire)",
        "s_infra": "État de l'infrastructure", "fill": "*(à compléter)*",
        "s_tickets": "Tickets / Demandes",
        "total": "Total capturé : **{tix} tickets**, **{inc} incidents**.",
        "tbl_head": "| Catégorie | Nombre | Résolution moyenne |",
        "tbl_sep": "|-----------|--------|--------------------|",
        "tbl_row": "| *(catégoriser)* | | |",
        "s_incidents": "Incidents", "s_actions": "Actions clés réalisées",
        "s_planned": "Prévu pour le mois prochain",
        "s_kpi": "Indicateurs ICT — ce mois",
        "kpi_hint": "*(disponibilité, test de restauration des sauvegardes, "
                    "tickets clôturés — à compléter)*",
        "footer": "*Brouillon auto-généré à partir de captures.jsonl "
                  "({n} élément(s) ouvert(s)). Prochain rapport : le 1er du "
                  "mois prochain.*",
        "nudge_subject": "Fin {month} {year}",
        "nudge_head": "🗓️ Fin {month} {year} :",
        "nudge_work": "• Travail : brouillon du rapport mensuel prêt ({tix} "
                      "tickets, {inc} incidents) — {url}",
        "nudge_ministry": "• Prédication : {hours}h enregistrées en {month}",
        "nudge_goal": " (objectif {goal:.0f}h).\n", "nudge_dot": ".\n",
        "nudge_secretary": "• Secrétaire : compiler et soumettre le rapport de "
                           "la congrégation pour {month} (totaux de prédication "
                           "dans Organized).",
    },
}
_L = _LABELS.get(MONTHEND_LANG, _LABELS["en"])
MONTHS = _MONTHS.get(MONTHEND_LANG, _MONTHS["en"])


def _die(msg: str) -> None:
    logger.error(f"Month-end pack ABORTED: {msg}")
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
        logger.warning(f"Month-end: cannot persist state: {e}")


def _prev_month(now: datetime) -> tuple[int, int, datetime, datetime]:
    """Return (year, month, start, end) of the previous calendar month."""
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first_this
    y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    start = first_this.replace(year=y, month=m)
    return y, m, start, end


def _target_notebook() -> str:
    from app.hooks.siyuan_tools import get_notebook_id
    return TRAVAY_NOTEBOOK if get_notebook_id(TRAVAY_NOTEBOOK) else LIFE_NOTEBOOK


def _render_report(y: int, m: int, start: datetime, end: datetime) -> tuple[str, int, int]:
    from app.graph.tools.capture import open_items

    tix = open_items(since=start, until=end)  # still-open items last month
    incidents = [r for r in tix if r.get("kind") == "incident"]
    tickets = [r for r in tix if r.get("kind") == "ticket"]

    # Translate the captured content into the report language (best-effort, one
    # batched call). English → passthrough, so en reports make no extra call.
    _orig = [r.get("text", "") for r in (incidents + tickets)]
    _tmap = dict(zip(_orig, _translate_texts(_orig, MONTHEND_LANG)))

    def _bullets(rows):
        return "\n".join(
            f"- {_tmap.get(r.get('text', ''), r.get('text', ''))}"
            for r in reversed(rows)) or "- —"

    site = f" — {WORK_SITE}" if WORK_SITE else ""
    title = _L["title"].format(site=site, month=MONTHS[m], year=y)
    md = f"""> {_L['draft']}

# {title}

**{_L['author']}:** {_L['role']}
**{_L['distribution']}**

## {_L['s_infra']}
{_L['fill']}

## {_L['s_tickets']}
{_L['total'].format(tix=len(tickets), inc=len(incidents))}

{_L['tbl_head']}
{_L['tbl_sep']}
{_L['tbl_row']}

## {_L['s_incidents']}
{_bullets(incidents)}

## {_L['s_actions']}
{_bullets(tickets)}

## {_L['s_planned']}
- …

## {_L['s_kpi']}
{_L['kpi_hint']}

---
{_L['footer'].format(n=len(tix))}
"""
    return md, len(tickets), len(incidents)


def main() -> None:
    now = datetime.now()
    y, m, start, end = _prev_month(now)
    month_key = f"{y:04d}-{m:02d}"
    force = os.getenv("MONTHEND_FORCE", "") == "1"

    state = _load_json(STATE_FILE)
    if state.get("month") == month_key and not force:
        logger.info(f"Month-end: {month_key} already done — skipping "
                    "(MONTHEND_FORCE=1 to regenerate)")
        return

    from app.graph.tools.capture import month_ministry_hours
    from app.hooks.siyuan_tools import (
        _siyuan_request, get_notebook_id, siyuan_doc_url, siyuan_upsert_page,
    )

    md, n_tix, n_inc = _render_report(y, m, start, end)
    notebook = _target_notebook()
    path = f"/📄 ICT Monthly/{month_key}"
    # Idempotency: nudge only on first creation (a lost state file re-running
    # in the same month must not re-nudge). upsert is safe either way.
    nb_id = get_notebook_id(notebook)
    existed = bool(nb_id and (_siyuan_request(
        "filetree/getIDsByHPath", {"notebook": nb_id, "path": path}, quiet=True
    ).get("data") or []))
    block_id = siyuan_upsert_page(notebook, path, md)
    if not block_id:
        _die(f"could not write monthly report draft to {notebook}{path}")
    url = siyuan_doc_url(block_id)

    hours = month_ministry_hours(y, m)
    _save_state({"month": month_key, "doc_id": block_id, "url": url,
                 "tickets": n_tix, "incidents": n_inc, "ministry_hours": hours})
    logger.info(f"Month-end OK: {notebook}{path} → {block_id} "
                f"(tix={n_tix}, inc={n_inc}, sèvis={hours}h, "
                f"{'refreshed' if existed else 'created'})")

    if existed:
        logger.info("Month-end: report already existed — no re-nudge (idempotent)")
        return

    # one scarce nudge covering all three roles
    try:
        from app.graph.tools.email import notify_admin
        from app.graph.tools.settings import get_setting
        goal = get_setting("ministry_goal_hours")  # typed number or None
        ministry_line = _L["nudge_ministry"].format(hours=hours, month=MONTHS[m])
        ministry_line += _L["nudge_goal"].format(goal=goal) if goal else _L["nudge_dot"]
        body = (
            _L["nudge_head"].format(month=MONTHS[m], year=y) + "\n"
            + _L["nudge_work"].format(tix=n_tix, inc=n_inc, url=url) + "\n"
            + ministry_line
        )
        if get_setting("secretary_reports"):
            body += _L["nudge_secretary"].format(month=MONTHS[m])
        if notify_admin(_L["nudge_subject"].format(month=MONTHS[m], year=y), body):
            logger.info("Month-end: nudge sent")
        else:
            logger.warning("Month-end: nudge not delivered")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Month-end: nudge error (draft still written): {e}")


if __name__ == "__main__":
    main()
