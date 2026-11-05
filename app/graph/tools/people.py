"""
👥 Shepherd — a light person model over the operator's `moun:` captures.

No new store: every `moun:`/person jot in captures.jsonl IS a contact touch.
This reads them back, groups them by person, and answers the shepherd's core
question — *"who have I gone quiet on?"* — plus a one-person history on demand.

A person jot may name the person up front, before a separator, so the touch
attaches to someone (the `person:` verb, or its Kreyòl alias `moun:`):

    person: Jean | studying, doing well
    person: Sò Marie - encouraged, back at the meetings
    person: Frè Paul #flock progressing slowly

The leading segment (before `|` / `—` / ` - ` / `:`) is the person; a short
jot with no separator is taken as just the name. `#tags` mark a role
(`#flock`, `#study`, `#shut-in`, …). A jot with no clear name is still logged
— it just won't attach to anyone, so it never surfaces in the shepherd view.

"Gone quiet" is deliberately conservative: only people you have an *ongoing*
relationship with (≥2 touches, or any role tag) and haven't logged in
`shepherd_quiet_days` (tunable from chat) show up — a single stray mention
never nags. Read-only; operator-only (toolset `capture`, never grantable).
"""

import os
import re
from datetime import datetime

from loguru import logger

# Honorifics stripped from the MATCH KEY only (display keeps them as written),
# so "Frè Jean" one day and "Jean" the next unify to one person.
_TITLES = {
    "fre", "frè", "so", "sò", "se", "sè", "brother", "bro", "sister", "sis",
    "elder", "frere", "soeur", "mr", "mrs", "ms", "sis.", "bro.",
}
_TAG_RE = re.compile(r"#([\w\-]+)", re.UNICODE)
# a separator between a leading name and the note
_SEP_RE = re.compile(r"\s*(?:\||—|:|\s-\s)\s*")


def _person_key(name: str) -> str:
    """Casefolded, title-stripped, punctuation-light key for grouping."""
    words = re.sub(r"[^\w\s\-]", " ", name or "", flags=re.UNICODE).split()
    kept = [w for w in words if w.casefold().strip(".") not in _TITLES]
    return " ".join(kept or words).casefold().strip()


def parse_person(text: str) -> tuple[str | None, str, list[str]]:
    """A person jot → (display_name | None, note, tags). The name is whatever
    comes before the first delimiter — a separator (`|`/`—`/`:`/` - `) OR the
    first `#tag`, whichever is earliest. With no delimiter, a short jot is a
    bare name and a long one is an unattached note (returns None)."""
    raw = (text or "").strip()
    tags = [t.casefold() for t in _TAG_RE.findall(raw)]
    if not raw:
        return None, "", tags

    # earliest boundary between the name and the note
    bounds = []
    if (m := _SEP_RE.search(raw)):
        bounds.append(m.start())
    if (m := _TAG_RE.search(raw)):
        bounds.append(m.start())
    cut = min(bounds) if bounds else None

    if cut is not None and raw[:cut].strip():
        name = raw[:cut].strip()
        note = _TAG_RE.sub("", raw[cut:]).lstrip(" |—:-").strip()
    else:
        # no usable boundary: a short line is a bare name, a long line is a note
        bare = _TAG_RE.sub("", raw).strip()
        name = bare if (bare and len(bare) <= 32 and len(bare.split()) <= 4) else None
        note = "" if name else bare
    if name and not _person_key(name):
        name = None
    return name, note, tags


def _touch(rec: dict) -> tuple[str | None, str, str, list[str]] | None:
    """A person capture record → (key, display_name, note, tags), preferring
    fields stored at capture time and falling back to parsing the text."""
    name = rec.get("person")
    tags = rec.get("tags")
    if name:
        note = rec.get("note") or rec.get("text") or ""
        tags = [str(t).casefold() for t in (tags or [])]
    else:
        name, note, tags = parse_person(rec.get("text") or "")
    if not name:
        return None
    key = _person_key(name)
    return (key, name.strip(), note, tags) if key else None


def people_index(now: datetime | None = None) -> list[dict]:
    """All named people, most-recently-touched first. Each entry:
    {key, name, last_ts, days_since, count, tags, last_note}."""
    from app.graph.tools.capture import read_captures
    now = now or datetime.now()
    people: dict[str, dict] = {}
    for rec in read_captures(kinds={"person"}):
        t = _touch(rec)
        if not t:
            continue
        key, name, note, tags = t
        try:
            when = datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            continue
        p = people.get(key)
        if p is None:
            p = people[key] = {
                "key": key, "name": name, "last_ts": when,
                "count": 0, "tags": set(), "last_note": note,
            }
        p["count"] += 1
        p["tags"].update(tags)
        if when >= p["last_ts"]:
            p["last_ts"], p["name"], p["last_note"] = when, name, note
    out = []
    for p in people.values():
        out.append({
            "key": p["key"], "name": p["name"],
            "last_ts": p["last_ts"].isoformat(timespec="seconds"),
            "days_since": (now - p["last_ts"]).days,
            "count": p["count"], "tags": sorted(p["tags"]),
            "last_note": p["last_note"],
        })
    return sorted(out, key=lambda p: p["days_since"])


def gone_quiet(now: datetime | None = None, days: int | None = None,
               min_touches: int = 2) -> list[dict]:
    """Established people (≥min_touches OR a role tag) not touched in `days`
    (default: the shepherd_quiet_days setting), longest-quiet first."""
    if days is None:
        try:
            from app.graph.tools.settings import get_setting
            days = get_setting("shepherd_quiet_days")
        except Exception:  # noqa: BLE001
            days = 21
    quiet = [
        p for p in people_index(now)
        if p["days_since"] >= days and (p["count"] >= min_touches or p["tags"])
    ]
    return sorted(quiet, key=lambda p: p["days_since"], reverse=True)


# ── Roster cross-reference (the shepherd pump) ───────────────────────────
#
# The shepherd view above is built purely from what you've LOGGED. This block
# crosses it with the congregation roster (Organized get_persons, the same
# source person_sync uses) so the cockpit can ask the sharper question:
# "who in your field-service group have you NOT logged / gone quiet on?" —
# turning the 182-person roster from a dead-end mirror into a capture primer.
#
# Everything here is read-only and degrades to empty when Organized/the roster
# is unavailable (the pump simply stays silent — never a hard failure).

def _quiet_days() -> int:
    try:
        from app.graph.tools.settings import get_setting
        return int(get_setting("shepherd_quiet_days") or 21)
    except Exception:  # noqa: BLE001
        return 21


def _roster_persons() -> list[dict]:
    """The active congregation roster (Organized backend, else a Hourglass
    export) — reusing person_sync's source resolution so 'the roster' has one
    definition. [] when no source is reachable."""
    try:
        from app.jobs.person_sync import (
            _persons_from_backend, _persons_from_hourglass,
        )
    except Exception:  # noqa: BLE001
        return []
    try:
        persons = _persons_from_backend() or _persons_from_hourglass()
    except Exception:  # noqa: BLE001
        return []
    return [p for p in persons if not p.get("archived")]


def _roster_key(person: dict) -> str:
    from app.jobs.person_sync import canonical_name
    return _person_key(canonical_name(person))


def _keys_match(a: str, b: str) -> bool:
    """True if one name-key's tokens subset the other's (shared, ≥2 chars).
    Tolerates a free-text 'Jean' matching roster 'Jean Toussaint Louis'. Bare,
    common first names stay deliberately loose — for the pump a false match
    just means 'assume logged' (under-nudge), which is the safe direction."""
    ta = {t for t in a.split() if len(t) >= 2}
    tb = {t for t in b.split() if len(t) >= 2}
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta


def _operator_name_key(persons: list[dict]) -> str | None:
    """The operator's roster name-key, via their ADMIN_PHONE RapidPro contact
    name — used to detect their group and exclude them from their own pump."""
    admin = [a for a in os.getenv("ADMIN_PHONE", "").split(",") if a.strip()]
    if not admin:
        return None
    phone = admin[0].split(":")[-1].lstrip("+")
    try:
        from app.graph.tools.rapidpro import _rp_api
        data = _rp_api("GET", "contacts.json", params={"urn": f"whatsapp:{phone}"})
        results = data.get("results", [])
        name = results[0].get("name") if results else None
    except Exception:  # noqa: BLE001
        name = None
    return _person_key(name) if name else None


def _operator_group_label(persons: list[dict], op_key: str | None) -> str | None:
    if not op_key:
        return None
    for p in persons:
        if _keys_match(op_key, _roster_key(p)):
            label = (p.get("group") or {}).get("label")
            if label:
                return label
    return None


def field_group_members(now: datetime | None = None) -> dict:
    """Resolve the operator's field-service group and its members from the
    roster. Returns {label, source, members:[{name,key,uid,role}]}. `label`
    comes from the `field_group` setting, else auto-detected from the operator's
    own roster record. Empty members when nothing resolves (a silent no-op)."""
    from app.graph.tools.settings import get_setting
    persons = _roster_persons()
    if not persons:
        return {"label": "", "source": "none", "members": []}
    op_key = _operator_name_key(persons)
    label = (get_setting("field_group") or "").strip()
    source = "setting"
    if not label:
        label = _operator_group_label(persons, op_key) or ""
        source = "auto"
    if not label:
        return {"label": "", "source": source, "members": []}
    from app.jobs.person_sync import canonical_name
    lk = label.casefold()
    members = []
    for p in persons:
        g = p.get("group") or {}
        if str(g.get("label", "")).casefold() != lk:
            continue
        nm = canonical_name(p)
        key = _person_key(nm)
        if op_key and _keys_match(op_key, key):
            continue  # don't nag the operator about themselves
        members.append({"name": nm, "key": key,
                        "uid": p.get("person_uid") or "", "role": g.get("role", "")})
    return {"label": label, "source": source, "members": members}


def group_coverage(now: datetime | None = None) -> dict:
    """Cross the operator's group against what they've logged. Returns
    {label, source, total, covered, missing[], stale[]} where `missing` are
    members with no touch ever and `stale` are members last touched ≥
    shepherd_quiet_days ago (longest-quiet first)."""
    now = now or datetime.now()
    fg = field_group_members(now)
    idx = people_index(now)
    quiet_days = _quiet_days()
    missing, covered = [], []
    for m in fg["members"]:
        best = None
        for lp in idx:
            if _keys_match(m["key"], lp["key"]):
                if best is None or lp["days_since"] < best["days_since"]:
                    best = lp
        if best is None:
            missing.append({**m, "days_since": None, "last_note": ""})
        else:
            covered.append({**m, "days_since": best["days_since"],
                            "last_note": best.get("last_note", "")})
    stale = sorted(
        (c for c in covered if c["days_since"] >= quiet_days),
        key=lambda c: c["days_since"], reverse=True,
    )
    return {
        "label": fg["label"], "source": fg["source"],
        "total": len(fg["members"]), "covered": len(covered),
        "missing": missing, "stale": stale,
    }


def group_coverage_lines(now: datetime | None = None, limit: int = 8) -> list[str]:
    """Renderable coverage for Home / the shepherd nudge. [] when no group
    resolves (so callers can stay silent rather than show an error)."""
    cov = group_coverage(now)
    if not cov["label"] or cov["total"] == 0:
        return []
    head = f"👥 **Field group {cov['label']}** — logged {cov['covered']}/{cov['total']}"
    neglected = cov["missing"] + cov["stale"]  # never-logged first, then quiet
    if not neglected:
        return [head, "- ✅ everyone's been in your notes lately."]
    out = [head]
    for m in neglected[:limit]:
        if m["days_since"] is None:
            out.append(f"- ⚪ **{m['name']}** — no touch logged yet")
        else:
            out.append(f"- 🕓 **{m['name']}** — {m['days_since']}d since last note")
    more = len(neglected) - min(len(neglected), limit)
    if more > 0:
        out.append(f"- …and {more} more")
    return out


def person_history(name: str, limit: int = 8,
                   now: datetime | None = None) -> list[dict]:
    """Recent touches for one person (key or substring match), newest first."""
    from app.graph.tools.capture import read_captures
    want = _person_key(name)
    sub = (name or "").casefold().strip()
    hits = []
    for rec in read_captures(kinds={"person"}):
        t = _touch(rec)
        if not t:
            continue
        key, disp, note, _tags = t
        if key == want or want in key or sub in disp.casefold():
            hits.append({"ts": rec.get("ts", ""), "name": disp,
                         "text": rec.get("text", "")})
    hits.sort(key=lambda h: h["ts"], reverse=True)
    return hits[:limit]


def _quiet_lines(quiet: list[dict]) -> list[str]:
    out = []
    for p in quiet:
        tag = f" [{', '.join(p['tags'])}]" if p["tags"] else ""
        out.append(f"{p['name']}{tag} — {p['days_since']}d since last contact")
    return out


def _quiet_text(now: datetime | None = None) -> str:
    quiet = gone_quiet(now)
    if not quiet:
        return "👥 Nobody's gone quiet — you're current with your people. 🙂"
    lines = ["👥 **Gone quiet** — reach out when you can:"]
    for p in quiet[:8]:
        tag = f" · _{', '.join(p['tags'])}_" if p["tags"] else ""
        note = f" — last: {p['last_note']}" if p["last_note"] else ""
        lines.append(f"- **{p['name']}** · {p['days_since']}d{tag}{note}")
    return "\n".join(lines)


def _list_text(now: datetime | None = None) -> str:
    idx = people_index(now)
    if not idx:
        return ("👥 No people logged yet. Jot one with "
                "`person: Jean | studying, doing well`.")
    lines = [f"👥 **Your people** ({len(idx)}):"]
    for p in idx[:20]:
        tag = f" · _{', '.join(p['tags'])}_" if p["tags"] else ""
        lines.append(f"- **{p['name']}** · last {p['days_since']}d ago · "
                     f"{p['count']} touch{'es' if p['count'] != 1 else ''}{tag}")
    return "\n".join(lines)


def _group_text(now: datetime | None = None) -> str:
    lines = group_coverage_lines(now)
    if not lines:
        return ("👥 No field-service group resolved yet. Tell me which is yours "
                "— `set field_group Gwoup 13` (the label from Organized) — or "
                "turn on Organized Data sharing so I can detect it.")
    lines.append("_Jot a touch:_ `person: Jean | encouraged, back at meetings`")
    return "\n".join(lines)


def _who_text(name: str, now: datetime | None = None) -> str:
    hist = person_history(name, now=now)
    if not hist:
        return f"👤 No touches logged for “{name}” yet."
    disp = hist[0]["name"]
    lines = [f"👤 **{disp}** — recent contacts:"]
    for h in hist:
        day = (h["ts"] or "")[:10]
        lines.append(f"- `{day}` — {h['text']}")
    return "\n".join(lines)


def macro_people(args: dict, **kwargs) -> str:
    """Shepherd view over the operator's person jots. The assistant maps
    natural language → structured args:
      who's gone quiet?      → {"action":"quiet"}
      my people / list       → {"action":"list"}
      my group / coverage    → {"action":"group"}
      who is Jean? / Jean    → {"action":"who","name":"Jean"}
    """
    action = (args.get("action") or "").strip().lower()
    name = (args.get("name") or "").strip()
    now = datetime.now()
    if action in ("who", "get", "history") or (name and not action):
        if not name:
            return "🤔 Who? e.g. `who is Jean`."
        return _who_text(name, now)
    if action in ("list", "all", "everyone"):
        return _list_text(now)
    if action in ("group", "coverage", "roster", "flock"):
        return _group_text(now)
    # default: the shepherd's question
    return _quiet_text(now)
