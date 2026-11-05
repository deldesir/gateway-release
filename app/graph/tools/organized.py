"""Organized backend tools.

Query the Organized scheduling backend for schedule data,
person assignments, and person lookups. Used by both the
Hermes AI agent (Layer 3) and RiveBot macro wrappers (Layer 2).
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)


def query_organized_api(args: dict, **kw) -> str:
    """Query the Organized scheduling backend.

    Actions:
      - get_schedule: Get schedule for a week
      - get_person_assignments: Get assignments for a person
      - search_persons: Search for persons by name

    Args:
        args: dict with 'action' and action-specific parameters

    Returns:
        JSON string with query results
    """
    action = args.get('action', '')
    if not action:
        return json.dumps({'error': 'No action specified'})

    payload = {
        'action': action,
        'congregation_id': args.get('congregation_id', ''),
        'week_of': args.get('week_of', ''),
        'person_name': args.get('person_name', ''),
        'query': args.get('query', ''),
    }
    # Pass through action-specific extras (e.g. weeks / include_past for
    # get_upcoming_talks) without enumerating every parameter here.
    for key, value in args.items():
        if key not in payload and value not in (None, ''):
            payload[key] = value

    organized_url = os.environ.get('ORGANIZED_URL', 'http://127.0.0.1:8088')
    webhook_secret = os.environ.get('ORGANIZED_WEBHOOK_SECRET', '')

    try:
        response = requests.post(
            f"{organized_url}/api/v3/webhooks/query",
            headers={
                'Content-Type': 'application/json',
                'X-Webhook-Secret': webhook_secret,
            },
            json=payload,
            timeout=10,
        )

        if response.status_code == 200:
            return json.dumps(response.json(), ensure_ascii=False)
        else:
            return json.dumps({
                'error': f'Organized API returned {response.status_code}',
                'message': response.json().get('message', ''),
            })

    except requests.RequestException as e:
        logger.error(f"Organized API query failed: {e}")
        return json.dumps({'error': f'Connection failed: {str(e)}'})

# ── RiveBot Macro Wrappers (Layer 2) ──────────────────────────────────────────

def _format_schedule(data: dict) -> str:
    """Format schedule JSON into WhatsApp-friendly text.

    The backend returns {"schedules": [...]}, where each schedule is a dict
    with at minimum a "weekOf" key.  Midweek/weekend sub-objects contain
    role assignments as {role: {"value": person_uid_or_name}}.
    """
    if "error" in data:
        return f"⚠️ {data['error']}"

    schedules = data.get("schedules", [])
    if not schedules:
        return "📅 No schedule data found."

    lines = ["📅 *Meeting Schedule*"]
    for entry in schedules:
        if not isinstance(entry, dict):
            continue
        week_of = entry.get("weekOf", entry.get("week_of", "Unknown"))
        lines.append(f"\n📆 *Week of {week_of}*")

        mw = entry.get("midweek", {})
        if isinstance(mw, dict) and mw:
            lines.append("📖 *Midweek Meeting*")
            for role, val in mw.items():
                if isinstance(val, dict) and val.get("value"):
                    lines.append(f"• {role.replace('_', ' ').title()}: {val['value']}")

        we = entry.get("weekend", {})
        if isinstance(we, dict) and we:
            lines.append("☀️ *Weekend Meeting*")
            for role, val in we.items():
                if isinstance(val, dict) and val.get("value"):
                    lines.append(f"• {role.replace('_', ' ').title()}: {val['value']}")

    return "\n".join(lines)


def macro_get_schedule(args: dict, **kwargs) -> str:
    """Get the meeting schedule for the current/specified week."""
    raw = query_organized_api({"action": "get_schedule"})
    data = json.loads(raw)
    return _format_schedule(data)


def macro_get_next_week(args: dict, **kwargs) -> str:
    """Get next week's meeting schedule.

    Calculates the Monday of next week and queries by week_of.
    """
    from datetime import datetime, timedelta
    today = datetime.now()
    # Monday of next week: advance to next Monday
    days_ahead = 7 - today.weekday()  # weekday() 0=Mon, so days to next Mon
    if days_ahead == 0:
        days_ahead = 7
    next_monday = today + timedelta(days=days_ahead)
    week_of = next_monday.strftime("%Y/%m/%d")

    raw = query_organized_api({"action": "get_schedule", "week_of": week_of})
    data = json.loads(raw)
    schedules = data.get("schedules", [])
    if not schedules:
        return f"📅 No schedule found for week of {week_of}."
    return _format_schedule(data)


def _next_own_talk(talks: list[dict], speaker: str, today) -> tuple[dict | None, int]:
    """(next own talk with its delivery Sunday, how many more after it).
    `week_of` is the week's Monday (YYYY/MM/DD or YYYY-MM-DD); the public
    talk is given on that week's Sunday (+6). Talks already delivered are
    ignored — the chat used to answer "next talk" with one given weeks ago."""
    from datetime import date as _date, timedelta as _td
    speaker = (speaker or "").strip().lower()
    mine = []
    for t in talks:
        who = (t.get("speaker") or "").lower()
        if not speaker or speaker not in who:
            continue
        raw = str(t.get("week_of") or "").replace("/", "-")[:10]
        try:
            monday = _date.fromisoformat(raw)
        except ValueError:
            continue
        sunday = monday + _td(days=6)
        if sunday < today:
            continue
        mine.append((sunday, t))
    mine.sort(key=lambda x: x[0])
    if not mine:
        return None, 0
    sunday, t = mine[0]
    return {**t, "delivery": sunday.isoformat(), "days_until": (sunday - today).days}, len(mine) - 1


def macro_my_next_talk(args: dict, **kwargs) -> str:
    """The operator's own next public talk: delivery Sunday, days left, S-34
    number and title when the sources are imported. Deterministic — no memory
    search, no guessing from old chats."""
    from app.graph.tools.capture import _local_now
    speaker = args.get("speaker") or os.getenv("TALK_SYNC_SPEAKER", "")
    if not speaker:
        return "⚠️ TALK_SYNC_SPEAKER is not set — I don't know which speaker you are."
    raw = query_organized_api({"action": "get_upcoming_talks", "weeks": args.get("weeks") or 16})
    data = json.loads(raw)
    if "error" in data:
        return f"⚠️ {data['error']}"
    today = _local_now().date()
    nxt, more = _next_own_talk(data.get("talks", []), speaker, today)
    if not nxt:
        return (f"🎤 No public talk assigned to you in the next {args.get('weeks') or 16} weeks "
                f"(Organized schedule, as of {today.isoformat()}).")
    no = nxt.get("talk_number") or "?"
    title = nxt.get("title") or nxt.get("theme") or ""
    days = nxt["days_until"]
    when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
    line = f"🎤 Your next public talk: Sunday {nxt['delivery']} ({when}) — No {no}"
    if title:
        line += f" · {title}"
    if more:
        line += f"\n(+{more} more scheduled after that)"
    return line


def macro_upcoming_talks(args: dict, **kwargs) -> str:
    """List upcoming weekend public talks (talk number, speaker, week).

    Backed by the get_upcoming_talks webhook action, which joins the
    Organized sources (talk number), schedules (speaker) and persons
    (display name) tables by week.
    """
    raw = query_organized_api({
        "action": "get_upcoming_talks",
        "weeks": args.get("weeks") or 8,
    })
    data = json.loads(raw)
    if "error" in data:
        return f"⚠️ {data['error']}"

    talks = data.get("talks", [])
    if not talks:
        return (
            "🎤 No upcoming public talks scheduled yet.\n"
            "Add the weekend program in Organized (Weekend Meeting page) "
            "and sync — talks will show up here."
        )

    lines = ["🎤 *Upcoming public talks*"]
    for t in talks:
        no = t.get("talk_number", "?")
        speaker = t.get("speaker") or "(speaker TBD)"
        kind = t.get("talk_type", "")
        kind_note = " (visiting)" if kind == "visitingSpeaker" else ""
        lines.append(f"• {t.get('week_of', '?')} — No {no}, {speaker}{kind_note}")
    return "\n".join(lines)


def macro_get_my_assignments(args: dict, **kwargs) -> str:
    """Get assignments for the current user.

    Attempts to resolve the caller's phone number to a person name
    via the RapidPro contact record, then queries for their assignments.
    Falls back to prompting for a manual search if resolution fails.
    """
    user_id = args.get("user_id", "")

    # Try to resolve phone → name via RapidPro contact
    if user_id:
        phone = user_id.split(":")[-1] if ":" in user_id else user_id
        from app.graph.tools.rapidpro import _rp_api
        contact_data = _rp_api("GET", "contacts.json", params={"urn": f"whatsapp:{phone}"})
        results = contact_data.get("results", [])
        if results and results[0].get("name"):
            name = results[0]["name"]
            raw = query_organized_api({
                "action": "get_person_assignments",
                "person_name": name,
            })
            data = json.loads(raw)
            assignments = data.get("assignments", [])
            if assignments:
                lines = [f"👤 *Your assignments ({name}):*\n"]
                for a in assignments:
                    lines.append(f"• {a.get('week_of', '?')}: {a.get('type', a.get('assignment_type', '?'))}")
                return "\n".join(lines)
            return f"👤 *{name}* — no upcoming assignments."

    return "📅 To see your assignments, type: *search [your name]*"


def macro_search_persons(args: dict, **kwargs) -> str:
    """Search for a person by name and show their assignments."""
    query = args.get("query", "").strip()
    if not query:
        return "🔍 Please provide a name to search. Example: *search john*"

    raw = query_organized_api({"action": "search_persons", "query": query})
    data = json.loads(raw)

    if "error" in data:
        return f"⚠️ {data['error']}"

    persons = data.get("persons", [])
    if not persons:
        return f"🤷‍♂️ No one found matching '{query}'."

    # Take the first match and fetch their assignments
    person = persons[0]
    person_name = person.get("display_name", "Unknown")

    raw_assignments = query_organized_api({
        "action": "get_person_assignments",
        "person_name": person_name,
    })
    assign_data = json.loads(raw_assignments)

    assignments = assign_data.get("assignments", [])
    if not assignments:
        return f"👤 *{person_name}*\nNo upcoming assignments."

    lines = [f"👤 *Assignments for {person_name}:*\n"]
    for a in assignments:
        lines.append(f"• {a.get('week_of', '?')}: {a.get('type', a.get('assignment_type', '?'))}")

    return "\n".join(lines)


# ── Flow UUID cache for macro_organized_menu ─────────────────────────────────
_organized_menu_uuid: str | None = None


def macro_organized_menu(args: dict, **kwargs) -> str:
    """Start the Organized Menu RapidPro flow.

    Caches the flow UUID after the first lookup to avoid a round-trip
    to the RapidPro API on every invocation.
    """
    global _organized_menu_uuid

    urn = args.get("user_id", "")
    if not urn:
        return "⚠️ Missing user context."
    if ":" not in urn:
        urn = f"whatsapp:{urn}"

    from app.graph.tools.rapidpro import _rp_api

    # Resolve flow UUID once, then cache
    if not _organized_menu_uuid:
        env_uuid = os.environ.get("ORGANIZED_MENU_FLOW_UUID", "")
        if env_uuid:
            _organized_menu_uuid = env_uuid
        else:
            flows = _rp_api("GET", "flows.json", params={"name": "organized_menu"})
            if "error" in flows:
                return f"⚠️ Menu flow not found: {flows['error']}"
            results = flows.get("results", [])
            if not results:
                return "⚠️ Menu flow 'organized_menu' not found."
            _organized_menu_uuid = results[0]["uuid"]
            logger.info(f"[organized] Cached menu flow UUID: {_organized_menu_uuid}")

    res = _rp_api("POST", "flow_starts.json", json={"flow": _organized_menu_uuid, "urns": [urn]})
    if "error" in res:
        # Invalidate cache in case flow was re-deployed with a new UUID
        _organized_menu_uuid = None
        return f"⚠️ Failed to start menu: {res['error']}"

    return "{{noreply}}"


# ── New Macros (ADR-012 Phase 3 expansion) ────────────────────────────────────

EVENT_CATEGORIES = {
    0: "🔄 CO Visit", 1: "🌿 Pioneer Week", 2: "🕊️ Special Campaign",
    3: "✝️ Memorial", 4: "🏛️ Convention", 5: "🏛️ Convention",
    6: "🌍 Int'l Convention", 7: "📚 Training", 8: "🔧 Hall Maintenance",
    9: "🏢 Bethel Tour", 10: "📋 Special Program", 11: "📢 Public Witnessing",
    12: "🏠 Kingdom Hall", 13: "🗣️ Language Course", 14: "📅 Annual Meeting",
    16: "📌 Custom",
}


def macro_get_events(args: dict, **kwargs) -> str:
    """Get upcoming congregation events."""
    raw = query_organized_api({"action": "get_events"})
    data = json.loads(raw)
    events = data.get("events", [])
    if not events:
        return "📆 No upcoming events."

    lines = ["📆 *Upcoming Events*\n"]
    for e in events:
        cat = EVENT_CATEGORIES.get(e.get("category", 16), "📌")
        start = e.get("start", "")[:10]
        lines.append(f"{cat} *{e.get('description', '?')}*")
        lines.append(f"   📅 {start}")
    return "\n".join(lines)


def macro_get_sources(args: dict, **kwargs) -> str:
    """Get meeting study material for the current week."""
    raw = query_organized_api({"action": "get_sources"})
    data = json.loads(raw)
    sources = data.get("sources", [])
    if not sources:
        return "📖 No source material found."

    # Show the first (current) week
    src = sources[0]
    lines = [
        f"📖 *Meeting Material — Week of {src.get('weekOf', '?')}*\n",
        f"📕 *Bible Reading:* {src.get('bible_reading', '—')}",
        f"💎 *TGW Talk:* {src.get('tgw_talk', '—')}",
        f"📗 *CBS:* {src.get('cbs', '—')}",
        f"📙 *WT Study:* {src.get('wt_study', '—')}",
    ]
    return "\n".join(lines)


def macro_get_field_group(args: dict, **kwargs) -> str:
    """Get the user's field service group."""
    user_id = args.get("user_id", "")
    person_name = ""

    # Resolve phone → name
    if user_id:
        phone = user_id.split(":")[-1] if ":" in user_id else user_id
        from app.graph.tools.rapidpro import _rp_api
        contact_data = _rp_api("GET", "contacts.json", params={"urn": f"whatsapp:{phone}"})
        results = contact_data.get("results", [])
        if results and results[0].get("name"):
            person_name = results[0]["name"]

    payload = {"action": "get_field_groups"}
    if person_name:
        payload["person_name"] = person_name

    raw = query_organized_api(payload)
    data = json.loads(raw)
    groups = data.get("groups", [])

    if not groups:
        if person_name:
            return f"👥 *{person_name}* is not assigned to any field service group."
        return "👥 No field service groups found."

    lines = []
    for g in groups:
        lines.append(f"👥 *{g.get('name', '?')}*\n")
        for m in g.get("members", []):
            role = f" {m['role']}" if m.get("role") else ""
            lines.append(f"  • {m.get('name', '?')}{role}")
        lines.append("")
    return "\n".join(lines).strip()


def macro_get_attendance(args: dict, **kwargs) -> str:
    """Get meeting attendance summary."""
    raw = query_organized_api({"action": "get_attendance"})
    data = json.loads(raw)
    attendance = data.get("attendance", [])
    if not attendance:
        return "📊 No attendance data available."

    lines = ["📊 *Meeting Attendance*\n"]
    for a in attendance:
        lines.append(f"📅 *{a.get('month', '?')}*")
        lines.append(f"  📖 Midweek avg: {a.get('avg_midweek', '?')}")
        lines.append(f"  ☀️ Weekend avg: {a.get('avg_weekend', '?')}")
    return "\n".join(lines)


def macro_get_field_report(args: dict, **kwargs) -> str:
    """Get the user's field service report."""
    user_id = args.get("user_id", "")
    person_name = ""

    if user_id:
        phone = user_id.split(":")[-1] if ":" in user_id else user_id
        from app.graph.tools.rapidpro import _rp_api
        contact_data = _rp_api("GET", "contacts.json", params={"urn": f"whatsapp:{phone}"})
        results = contact_data.get("results", [])
        if results and results[0].get("name"):
            person_name = results[0]["name"]

    if not person_name:
        return "📋 To see your report, make sure your contact name is set."

    raw = query_organized_api({
        "action": "get_field_report",
        "person_name": person_name,
    })
    data = json.loads(raw)
    reports = data.get("reports", [])

    if not reports:
        return f"📋 *{person_name}* — no field service reports found."

    lines = [f"📋 *Field Service Reports ({person_name}):*\n"]
    for r in reports:
        status = "✅" if r.get("status") == "confirmed" else "⏳"
        lines.append(
            f"{status} *{r.get('month', '?')}* — "
            f"{r.get('hours', 0)}h · {r.get('bible_studies', 0)} studies"
        )
    return "\n".join(lines)


def macro_get_visiting_speakers(args: dict, **kwargs) -> str:
    """Get visiting speakers and their talk outlines."""
    raw = query_organized_api({"action": "get_visiting_speakers"})
    data = json.loads(raw)
    speakers = data.get("speakers", [])
    if not speakers:
        return "🎤 No visiting speakers registered."

    lines = ["🎤 *Visiting Speakers*\n"]
    for s in speakers:
        role = "🧓 Elder" if s.get("elder") else "📋 MS"
        lines.append(f"*{s.get('name', '?')}* — {role}")
        for t in s.get("talks", []):
            lines.append(f"  📝 #{t.get('number', '?')}: {t.get('title', '')}")
    return "\n".join(lines)


def macro_get_speakers_congregations(args: dict, **kwargs) -> str:
    """Get partner congregations for speaker exchange."""
    raw = query_organized_api({"action": "get_speakers_congregations"})
    data = json.loads(raw)
    congs = data.get("congregations", [])
    if not congs:
        return "🏛️ No partner congregations registered."

    lines = ["🏛️ *Partner Congregations*\n"]
    for c in congs:
        lines.append(f"*{c.get('name', '?')}* ({c.get('number', '')})")
        lines.append(f"  📍 {c.get('address', '—')}")
        lines.append(f"  🔄 Circuit: {c.get('circuit', '—')}")
        lines.append(f"  🎤 Talk coord: {c.get('talk_coordinator', '—')}")
        lines.append(f"  ☀️ Weekend: {c.get('weekend_time', '—')}")
        lines.append("")
    return "\n".join(lines).strip()


def macro_get_cong_report(args: dict, **kwargs) -> str:
    """Get congregation field service report aggregate."""
    raw = query_organized_api({"action": "get_cong_report"})
    data = json.loads(raw)
    reports = data.get("reports", [])
    if not reports:
        return "📊 No congregation reports available."

    lines = ["📊 *Congregation Service Report*\n"]
    for r in reports:
        lines.append(
            f"✅ *{r.get('month', '?')}* — "
            f"{r.get('hours', 0)}h · {r.get('bible_studies', 0)} studies"
        )
    return "\n".join(lines)


def macro_get_branch_report(args: dict, **kwargs) -> str:
    """Get branch field service report summary."""
    raw = query_organized_api({"action": "get_branch_report"})
    data = json.loads(raw)
    reports = data.get("reports", [])
    if not reports:
        return "📈 No branch reports available."

    lines = ["📈 *Branch Service Report*\n"]
    for r in reports:
        status = "✅" if r.get("submitted") else "⏳"
        lines.append(f"{status} *{r.get('month', '?')}*")
        lines.append(f"  👥 Active publishers: {r.get('publishers_active', 0)}")
        lines.append(f"  📋 Reporting: {r.get('publishers_reporting', 0)}")
        lines.append(f"  📖 Bible studies: {r.get('total_bible_studies', 0)}")
        lines.append(f"  ☀️ Weekend avg: {r.get('meeting_avg', 0)}")
        lines.append(f"  🏃 AP hours: {r.get('ap_hours', 0)}")
    return "\n".join(lines)


def macro_get_delegated_reports(args: dict, **kwargs) -> str:
    """Get delegated field service reports."""
    raw = query_organized_api({"action": "get_delegated_reports"})
    data = json.loads(raw)
    reports = data.get("reports", [])
    if not reports:
        return "📝 No delegated reports."

    lines = ["📝 *Delegated Reports*\n"]
    for r in reports:
        status = "✅" if r.get("status") == "confirmed" else "⏳"
        lines.append(
            f"{status} *{r.get('person', '?')}* ({r.get('month', '?')}) — "
            f"{r.get('hours', 0)}h"
        )
        if r.get("comments"):
            lines.append(f"  💬 {r['comments']}")
    return "\n".join(lines)


def macro_get_cong_analysis(args: dict, **kwargs) -> str:
    """Get congregation analysis summary."""
    raw = query_organized_api({"action": "get_cong_analysis"})
    data = json.loads(raw)
    analysis = data.get("analysis", [])
    if not analysis:
        return "📋 No congregation analysis available."

    lines = ["📋 *Congregation Analysis*\n"]
    for a in analysis:
        status = "✅" if a.get("submitted") else "⏳"
        lines.append(f"{status} *{a.get('month', '?')}*")
        lines.append(f"  📖 Midweek avg: {a.get('midweek_avg', 0)}")
        lines.append(f"  ☀️ Weekend avg: {a.get('weekend_avg', 0)}")
        lines.append(
            f"  👥 Publishers: {a.get('active', 0)} active · "
            f"{a.get('inactive', 0)} inactive · "
            f"{a.get('reactivated', 0)} reactivated"
        )
        lines.append(
            f"  🗺️ Territories: {a.get('territories_uncovered', 0)}/"
            f"{a.get('territories_total', 0)} uncovered"
        )
    return "\n".join(lines)


def macro_get_bible_studies(args: dict, **kwargs) -> str:
    """Get user's bible studies."""
    raw = query_organized_api({"action": "get_bible_studies"})
    data = json.loads(raw)
    studies = data.get("studies", [])
    if not studies:
        return "📖 No bible studies registered."

    lines = [f"📖 *My Bible Studies ({len(studies)}):*\n"]
    for s in studies:
        lines.append(f"  • {s.get('name', '?')}")
    return "\n".join(lines)


def macro_get_notifications(args: dict, **kwargs) -> str:
    """Get user's notifications."""
    user_id = args.get("user_id", "")
    person_name = ""

    if user_id:
        phone = user_id.split(":")[-1] if ":" in user_id else user_id
        from app.graph.tools.rapidpro import _rp_api
        contact_data = _rp_api("GET", "contacts.json", params={"urn": f"whatsapp:{phone}"})
        results = contact_data.get("results", [])
        if results and results[0].get("name"):
            person_name = results[0]["name"]

    payload = {"action": "get_notifications"}
    if person_name:
        payload["person_name"] = person_name

    raw = query_organized_api(payload)
    data = json.loads(raw)
    notifs = data.get("notifications", [])

    if not notifs:
        return "🔔 No notifications."

    lines = ["🔔 *Notifications*\n"]
    for n in notifs:
        icon = "🔴" if not n.get("read") else "⚪"
        lines.append(f"{icon} *{n.get('title', '?')}*")
        lines.append(f"  {n.get('body', '')}")
    return "\n".join(lines)
