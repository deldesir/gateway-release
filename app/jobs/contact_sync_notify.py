"""📇 Team contact notifier — push directory changes out to each team member.

Watches every directory node (a `Team:` and/or `Directory:` RapidPro group) and,
when its directory changes, sends the new/changed cards to everyone entitled to
it — the holders of a `Team:`/`Access:` grant on that node, above it, or below it.
Each node is versioned on its own, so one edit is announced once, not once per
site that happens to see it.

**Polling, not an event hook.** The operator adds contacts wherever is
convenient — the RapidPro UI, `person_sync`, an import — and this still notices.
An event fired from our own code path would miss every one of those.

Change detection is a hash of the rendered directory (`rev` excluded, so the
generator's output is stable). When the hash moves, the team's version
increments and the delta is computed against the cards we last saw:

    version 17 → 18, added: Jane · changed: Bob's number · removed: Ken

Delta, not the whole directory: re-importing a full .vcf is the most reliable
way to create duplicates on a phone, since UID honouring is inconsistent. A
member who has never been notified gets the full directory once; after that,
only what changed. **Removals are advisory** — a .vcf cannot delete anything, so
the member is told to delete the contact by hand.

Safe by construction, same gate as the TWP notifier:
  - DEFAULT (review): emails the OPERATOR the drafts and who each would go to.
    Nothing reaches a third party.
  - AUTOSEND (CONTACT_SYNC_AUTOSEND=1): delivers to each member on their own
    channel and emails the operator a per-person summary.
  - DRY (CONTACT_SYNC_DRY=1): print what would happen, touch nothing.

Deltas are per member: every version's cards are kept, so a member who is
several versions behind (a failed delivery, a review-only period, an on-demand
`sync` in between) receives exactly what changed since *their* copy, never the
whole directory again. An on-demand `sync` is recorded here too
(`record_on_demand`), so the next run does not send its requester a second full
copy.

Two ways to run it: the daily timer, which only acts when something changed, and
the operator's `push contacts` command (`macro_sync_push`), which delivers the
pending updates at once whatever the mode.

Idempotent: a member is recorded at the version they were delivered, so a
re-run sends nothing new. Failures are deliberately NOT recorded, so the next
run retries just those; the job then exits non-zero so systemd's OnFailure=
emails the operator. CONTACT_SYNC_FORCE=1 ignores state for a re-run.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.contact_sync_notify
"""

import contextlib
import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from loguru import logger

from app.graph.tools.contact_sync import (
    DEFAULT_LANG,
    _deliver_async,
    _email_of,
    _phone_of,
    can_reach,
    _rev,
    _slug,
    _t,
    collect_contacts,
    deliver_to_member,
    list_teams,
    level_display,
    list_nodes,
    localize,
    node_recipients,
    team_label,
    whatsapp_push_allowed,
)
from app.utils.vcard import build_vcard, build_vcf

STATE_FILE = os.getenv(
    "CONTACT_SYNC_STATE_FILE", "/opt/iiab/ai-gateway/data/contact_sync_state.json"
)
# How many past versions of each directory to keep for per-member deltas. A
# member further behind than that simply gets the full directory once.
HISTORY_KEEP = int(os.getenv("CONTACT_SYNC_HISTORY", "30"))


class NotifierError(RuntimeError):
    """The run could not finish its job (e.g. the operator's review email failed)."""


def _die(msg: str) -> None:
    logger.error(f"Contact sync notifier: {msg}")
    sys.exit(1)


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    # Atomic (tmp + rename, same as access._save): a torn write here would make
    # _load_json return {}, resetting every version — and a version reset means
    # the full directory is re-sent to everyone, i.e. phone duplicates.
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


@contextlib.contextmanager
def _locked():
    """Serialize every read-modify-write of the state file.

    Two writers exist: this job (the timer, or the operator's push) and the
    on-demand `sync`, which records what it delivered. Without the lock, one
    would silently overwrite the other's record.
    """
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _card_hash(contact) -> str:
    """Fingerprint one contact, ignoring REV so it only moves on real edits."""
    return hashlib.sha1(build_vcard(contact).encode("utf-8")).hexdigest()[:12]


def _card_record(contact) -> dict:
    """What we remember per contact: its fingerprint, plus the name.

    The name is stored so a *removed* contact can still be named in the advisory
    message — by the time we notice they are gone, they are no longer in RapidPro
    to look up.
    """
    return {"h": _card_hash(contact), "n": contact.display_name}


def _hash_of(record) -> str:
    """Read a stored fingerprint, tolerating an older bare-hash entry."""
    return record.get("h", "") if isinstance(record, dict) else str(record or "")


def _name_of(record) -> str:
    return (record.get("n") or "") if isinstance(record, dict) else ""


def _directory_hash(cards: dict) -> str:
    joined = "|".join(f"{uid}:{_hash_of(rec)}" for uid, rec in sorted(cards.items()))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def observe(state: dict, group: str, directory: list) -> tuple[dict, dict]:
    """Record a node's current directory and bump its version when it changed.

    Every version's cards are kept (the last HISTORY_KEEP), which is what lets a
    member several versions behind get exactly what changed since their copy.
    """
    cards = {c.uid: _card_record(c) for c in directory}
    prior = state.setdefault("teams", {}).setdefault(
        group, {"version": 0, "hash": "", "cards": {}, "delivered": {}, "reviewed": {}})
    history = prior.setdefault("history", {})
    current = _directory_hash(cards)
    if current != prior.get("hash"):
        # A state file written before history existed still knows the cards of
        # its current version; keep them, members may be holding exactly that.
        if prior.get("version") and prior.get("cards") and str(prior["version"]) not in history:
            history[str(prior["version"])] = prior["cards"]
        prior["version"] = int(prior.get("version", 0)) + 1
        logger.info(f"Contact sync: {team_label(group)} changed → version {prior['version']}")
    prior["hash"] = current
    prior["cards"] = cards
    history[str(prior["version"])] = cards
    for old in sorted(history, key=int)[:-HISTORY_KEEP]:
        del history[old]
    return prior, cards


def _plan(prior: dict, cards: dict, directory: list, uid: str, delivered: int):
    """What one member needs now: (kind, payload, removed_names).

    Never their own card. A first-timer, or someone whose version is no longer in
    the history, gets the full directory; everyone else gets the difference
    between their version and now.
    """
    mine = [c for c in directory if c.uid != uid]
    base = (prior.get("history") or {}).get(str(delivered)) if delivered else None
    if base is None:
        return "full", mine, []
    payload = [c for c in mine if _hash_of(base.get(c.uid)) != _hash_of(cards[c.uid])]
    removed = [_name_of(rec) or f"a contact ({u[:8]})"
               for u, rec in base.items() if u not in cards and u != uid]
    return "delta", payload, removed


def record_on_demand(member_uid: str, label: str, directories: dict) -> None:
    """Remember that a member just received these directories in full, on request.

    `directories` maps each node's team key to its complete listing (the member's
    own card included), exactly as this job would see it. Without this record the
    next run would meet a member it never served and send them everything again:
    a second full import, which is how phones fill up with duplicates.
    """
    if not member_uid or not directories:
        return
    try:
        with _locked():
            state = _load_json(STATE_FILE)
            for group, directory in directories.items():
                if directory:
                    prior, _ = observe(state, group, directory)
                    prior.setdefault("delivered", {})[member_uid] = prior["version"]
            state.setdefault("on_demand", {}).setdefault(member_uid, {})[label] = (
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
            _save_state(state)
    except Exception as exc:  # the file went out; failing to note it must not hide that
        logger.warning(f"[contact_sync] could not record an on-demand delivery: {exc}")


def smart_plan(member_uid: str, listings: dict) -> tuple[list, list[str], bool]:
    """What a member is missing across these directories, from their recorded copies.

    Returns (cards, removed_names, first_time). A directory they never received
    comes whole; one they hold an older version of comes as the difference; one
    they hold at the current version contributes nothing. `first_time` is True
    only when every directory comes whole.
    """
    cards: dict = {}
    removed: list[str] = []
    first_time = True
    with _locked():
        state = _load_json(STATE_FILE)
        for group, directory in listings.items():
            if not directory:
                continue
            prior, current = observe(state, group, directory)
            delivered = int((prior.get("delivered") or {}).get(member_uid, 0))
            if delivered >= prior["version"]:
                first_time = False
                continue
            kind, payload, gone = _plan(prior, current, directory, member_uid, delivered)
            first_time = first_time and kind == "full"
            for card in payload:
                cards.setdefault(card.uid, card)
            removed += [name for name in gone if name not in removed]
        _save_state(state)
    return list(cards.values()), removed, first_time


def stamp_on_demand(member_uid: str, key: str) -> None:
    """Note an on-demand send that is not a full copy (a search, a department),
    so a repeat within the cooldown is answered instead of sent again."""
    if not member_uid:
        return
    try:
        with _locked():
            state = _load_json(STATE_FILE)
            state.setdefault("on_demand", {}).setdefault(member_uid, {})[key] = (
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
            _save_state(state)
    except Exception as exc:
        logger.warning(f"[contact_sync] could not stamp an on-demand send: {exc}")


def lang_pref(member_uid: str) -> str:
    """The language a member chose in the menu, or ''."""
    return ((_load_json(STATE_FILE).get("prefs") or {}).get(member_uid) or {}).get("lang", "")


def get_pref(member_uid: str, key: str) -> str:
    return str(((_load_json(STATE_FILE).get("prefs") or {}).get(member_uid) or {}).get(key) or "")


def set_pref(member_uid: str, key: str, value: str) -> None:
    with _locked():
        state = _load_json(STATE_FILE)
        state.setdefault("prefs", {}).setdefault(member_uid, {})[key] = value
        _save_state(state)


def set_lang_pref(member_uid: str, lang: str) -> None:
    """Remember a member's language choice. Kept here, not in RapidPro: this
    feature never writes to RapidPro."""
    with _locked():
        state = _load_json(STATE_FILE)
        state.setdefault("prefs", {}).setdefault(member_uid, {})["lang"] = lang
        _save_state(state)


def last_on_demand(member_uid: str, label: str):
    """When this member last received `label` on request, or None."""
    stamp = ((_load_json(STATE_FILE).get("on_demand") or {}).get(member_uid) or {}).get(label)
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def run(autosend: bool | None = None, dry: bool | None = None, force: bool | None = None) -> dict:
    """One notification pass. Returns a summary; raises NotifierError if the
    operator's review email could not be sent.

    Arguments left as None fall back to CONTACT_SYNC_AUTOSEND / _DRY / _FORCE.
    """
    dry = os.getenv("CONTACT_SYNC_DRY", "") == "1" if dry is None else dry
    force = os.getenv("CONTACT_SYNC_FORCE", "") == "1" if force is None else force
    autosend = os.getenv("CONTACT_SYNC_AUTOSEND", "").strip() == "1" if autosend is None else autosend
    summary = {"mode": "idle", "sent": [], "failed": [], "unreachable": [], "drafted": 0}

    teams = list_nodes()
    if not teams:
        logger.info("Contact sync: no 'Team:' or 'Directory:' groups exist — nothing to do")
        return summary

    with _locked():
        state = {} if force else _load_json(STATE_FILE)
        state.setdefault("teams", {})
        packets = _build_packets(state, teams, autosend)

        if not packets:
            if not dry:
                _save_state(state)
            logger.info("Contact sync: every member is up to date")
            return summary

        for packet in packets:
            packet["subject"], packet["body"] = _compose(packet)
            packet["filename"] = (
                f"{_slug(packet['label'])}-contacts.vcf" if packet["kind"] == "full"
                else f"{_slug(packet['label'])}-new-contacts.vcf"
            )
            # Fingerprints are language-neutral; the file itself is titled for its reader.
            packet["vcf"] = build_vcf(localize(packet["payload"], packet["lang"]), rev=_rev())

        if dry:
            _print_dry(packets, autosend)
            summary["mode"] = "dry"
            return summary

        from app.graph.tools.email import notify_admin

        if autosend:
            summary["mode"] = "autosend"
            _deliver_packets(state, packets, summary)
            _save_state(state)
            notify_admin("Contact sync — updates sent", _summary_text(summary))
            logger.info(f"Contact sync autosend: {len(summary['sent'])} sent, "
                        f"{len(summary['failed'])} failed, {len(summary['unreachable'])} unreachable")
            return summary

        summary["mode"] = "review"
        head = (f"📇 Contact sync — {len(packets)} member(s) to notify.\n\n"
                "These are ready to send. Verify them, then send `push contacts` to "
                "deliver them now (or set CONTACT_SYNC_AUTOSEND=1 to deliver on every change).\n")
        blocks = [head]
        for p in packets:
            reach = _email_of(p["member"]) or (_phone_of(p["member"]) or ["⚠️ no phone or email"])[0]
            blocks.append("═══════════════\n"
                          f"👤 {p['name']} — {p['label']} v{p['version']} ({p['kind']})\n"
                          f"📧 {reach}\n"
                          f"Subject: {p['subject']}\n\n{p['body']}\n"
                          f"[{len(p['payload'])} vCard(s) would be attached as {p['filename']}]")
            # `reviewed`, not `delivered`: the operator has seen it, but nothing
            # has reached the member yet — a push must still send this.
            state["teams"][p["group"]].setdefault("reviewed", {})[p["member_uid"]] = p["version"]
        if not notify_admin("Contact sync — updates to review", "\n\n".join(blocks)):
            raise NotifierError("could not deliver the contact-sync review packet to the operator")
        _save_state(state)
        summary["drafted"] = len(packets)
        logger.info(f"Contact sync review: {len(packets)} draft(s) sent to operator")
        return summary


def _build_packets(state: dict, teams: list, autosend: bool) -> list[dict]:
    packets: list[dict] = []
    for group in teams:
        label = team_label(group)
        directory = collect_contacts(group)  # everyone, including each member
        if not directory:
            if not group.endswith("#mission"):  # an empty mission-wide subset is normal
                logger.info(f"Contact sync: {label} is empty — skipped")
            continue
        prior, cards = observe(state, group, directory)
        version = prior["version"]

        for member in node_recipients(group):
            uid = member.get("uuid")
            if not uid:
                continue
            delivered = int((prior.get("delivered") or {}).get(uid, 0))
            reviewed = int((prior.get("reviewed") or {}).get(uid, 0))
            if delivered >= version:
                continue  # already holds this version
            if reviewed >= version and not autosend:
                continue  # already shown to the operator; a push still sends it
            # An unreachable member stays pending forever; without this every
            # run would report the same "unreachable" again. Reachability is
            # re-checked first, so adding their email un-sticks them at once.
            if (not can_reach(member, whatsapp_push_allowed(member))
                    and int((prior.get("unreachable") or {}).get(uid, 0)) >= version):
                continue

            kind, payload, removed = _plan(prior, cards, directory, uid, delivered)
            if not payload and not removed:
                # Nothing actionable (e.g. the only change was their own card):
                # record the version so we stop re-checking.
                prior.setdefault("delivered", {})[uid] = version
                continue

            lang = (((state.get("prefs") or {}).get(uid) or {}).get("lang")
                    or member.get("language") or "")
            packets.append({
                "group": group, "label": level_display(group, lang or DEFAULT_LANG), "version": version,
                "member": member, "member_uid": uid,
                "name": (member.get("name") or "").strip() or "(no name)",
                "payload": payload, "kind": kind, "removed_names": removed,
                "lang": lang,
            })
    return packets


def _deliver_packets(state: dict, packets: list[dict], summary: dict) -> None:
    for p in packets:
        # No usable channel is a data/config gap (no email on file, or no WhatsApp
        # channel token, so a phone cannot carry a file). Reporting it as a failure
        # would re-alert on every run with no possible success.
        whatsapp_ok = whatsapp_push_allowed(p["member"])
        if not can_reach(p["member"], whatsapp_ok):
            state["teams"][p["group"]].setdefault("unreachable", {})[p["member_uid"]] = p["version"]
            summary["unreachable"].append(p)
            continue
        ok, via = deliver_to_member(p["member"], p["vcf"], p["filename"], p["subject"], p["body"],
                                    whatsapp_ok=whatsapp_ok)
        if ok:
            p["via"] = via
            state["teams"][p["group"]].setdefault("delivered", {})[p["member_uid"]] = p["version"]
            summary["sent"].append(p)
        else:
            summary["failed"].append(p)  # not recorded → the next run retries just these


def _summary_text(summary: dict) -> str:
    lines = [f"📇 Contact sync — {len(summary['sent'])} sent, {len(summary['failed'])} failed, "
             f"{len(summary['unreachable'])} unreachable.\n"]
    lines += [f"✅ {p['name']} · {p['label']} v{p['version']} ({p['kind']}, {len(p['payload'])}) via {p['via']}"
              for p in summary["sent"]]
    lines += [f"❌ {p['name']} · {p['label']} v{p['version']} — delivery failed" for p in summary["failed"]]
    lines += [f"⚠️ {p['name']} · {p['label']} — {_why_unreachable(p['member'])}" for p in summary["unreachable"]]
    return "\n".join(lines)


def _print_dry(packets: list[dict], autosend: bool) -> None:
    for p in packets:
        reach = _email_of(p["member"]) or (_phone_of(p["member"]) or ["⚠️ unreachable"])[0]
        print(f"\n=== {p['name']} · {p['label']} v{p['version']} "
              f"· {p['kind']} ({len(p['payload'])} card(s)) → {reach} ===")
        print(f"Subject: {p['subject']}")
        print(p["body"])
    print(f"\n[DRY] {len(packets)} notification(s); autosend={autosend}; nothing sent, state untouched.")


def main() -> None:
    try:
        summary = run()
    except NotifierError as exc:
        _die(str(exc))
        return
    if summary["failed"]:
        _die(f"{len(summary['failed'])} contact notification(s) failed to send")


def macro_sync_push(args: dict, **kwargs) -> str:
    """Operator command: deliver pending directory updates now.

    Same pass as the daily check, but delivering whatever the configured mode,
    so the operator decides when updates go out. Only what changed since each
    member's copy is sent. Answers at once; the summary arrives by email.
    """
    lang = (args or {}).get("lang") or DEFAULT_LANG

    def _push() -> None:
        from app.graph.tools.email import notify_admin
        try:
            summary = run(autosend=True, dry=False)
            if summary["mode"] == "idle":
                notify_admin("Contact sync — nothing to send",
                             "📇 Every member already has the current directory. Nothing was sent.")
        except Exception:
            logger.exception("[contact_sync] PUSH_CRASHED")
            notify_admin("Contact sync — push failed",
                         "📇 The push stopped with an error; see the ai-gateway journal.")

    logger.info("[contact_sync] PUSH_REQUESTED")
    _deliver_async(_push)
    return _t("push_started", lang)


def _why_unreachable(member: dict) -> str:
    """Tell the operator what to fix, not just that it did not work."""
    if _phone_of(member) and not whatsapp_push_allowed(member):
        return ("has not written to the directory number yet: WhatsApp updates start after "
                "their first message (for example `sync`), or add an email")
    if _phone_of(member):
        return "has a phone but no email, and there is no WhatsApp channel token so WhatsApp cannot carry a file"
    return "no phone or email on file"


def _compose(packet: dict) -> tuple[str, str]:
    lang = packet["lang"]
    label, n = packet["label"], len(packet["payload"])
    names = ", ".join(c.display_name for c in packet["payload"][:8])
    if len(packet["payload"]) > 8:
        names += ", …"

    subject = _t("subject", lang, team=label)
    if not packet["payload"]:
        # Removal-only: no file rides along, so no "open the attachment" talk.
        body = ""
    elif packet["kind"] == "full":
        body = _t("body", lang, n=n, team=label)
    else:
        body = _t("delta_body", lang, team=label, names=names)

    if packet["removed_names"]:
        body += _t("removed_note", lang, names=", ".join(packet["removed_names"]))
    return subject, body.strip()


if __name__ == "__main__":
    main()
