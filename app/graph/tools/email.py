"""
send_email — cross-channel delivery: reply to a WhatsApp (or any) session
by email when the content suits email better (explicit "email me this"
intents, long reports, exports).

Transport: a RapidPro broadcast to the recipient's mailto: URN. Courier
routes it through the EX email channel → email-bridge → SMTP, so every
outbound email shows up in the contact's RapidPro history with delivery
status, retries, and audit trail — unlike a direct SMTP call.

Privacy invariants:
  - The caller's identity is read server-side (contextvar), never from
    LLM-supplied arguments.
  - Non-admin callers can only email THEMSELVES (their own mailto: URN on
    file in RapidPro). The `to` override is honored for the admin only.
  - No address is ever auto-attached from chat text.
"""

import hashlib
import logging
import os

from app.hermes.engine import _current_urn
from app.graph.tools.rapidpro import _rp_api, get_client

logger = logging.getLogger(__name__)

# Report nudges go STRAIGHT to the email-bridge, not through a RapidPro
# broadcast: RapidPro resolves a broadcast to the contact's highest-priority
# URN (the operator's WhatsApp), so a mailto broadcast never reaches email.
EMAIL_BRIDGE_SEND_URL = os.getenv("EMAIL_BRIDGE_SEND_URL", "").strip()
EMAIL_BRIDGE_TOKEN = os.getenv("EMAIL_BRIDGE_TOKEN", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()

# RapidPro caps broadcast text at 4096 chars (temba MAX_TEXT_LEN); leave
# headroom for the in-band Subject line and part marker.
_CHUNK_SIZE = 3900
_MAX_PARTS = 5


def _sanitize_tail(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value)


def _admin_ids() -> list[str]:
    return [a.strip() for a in os.getenv("ADMIN_PHONE", "").split(",") if a.strip()]


def _is_admin(urn: str) -> bool:
    """Same identity rule as app.hermes.tools._is_admin_caller (kept local:
    hermes.tools imports this package, so importing back would cycle)."""
    tail = _sanitize_tail(urn.split(":")[-1].lstrip("+"))
    admin_tails = [_sanitize_tail(a) for a in _admin_ids()]
    return bool(admin_tails) and tail in admin_tails


def _admin_email() -> str | None:
    for entry in _admin_ids():
        if "@" in entry:
            return entry
    return None


def admin_notify_target(
    subject: str, text: str, channel: str | None = None
) -> tuple[str, str] | None:
    """URN + message text for a proactive admin notification (nightly jobs).

    Email-first by design: scheduled reports (talk nudge, talk-sync notices)
    go to the admin's mailto: URN with an in-band Subject: line the email
    bridge lifts — WhatsApp stays reserved for conversation. Set
    ADMIN_NOTIFY_CHANNEL=whatsapp to route them to the admin's number
    instead. Returns None when no target of the chosen kind exists.

    `channel` overrides ADMIN_NOTIFY_CHANNEL for THIS call — used by the
    reminders job to force 'whatsapp' (a personal ping the operator set
    themselves belongs on their phone, not buried in email; scheduled
    reports stay email-first).
    """
    channel = (channel or os.getenv("ADMIN_NOTIFY_CHANNEL", "email")).strip().lower()

    if channel != "whatsapp":
        addr = _admin_email()
        if addr:
            return f"mailto:{addr}", f"Subject: {subject}\n{text}"
        logger.warning(
            "admin_notify_target: no email in ADMIN_PHONE — "
            "falling back to WhatsApp"
        )

    for entry in _admin_ids():
        if "@" not in entry:
            return f"whatsapp:{entry.split(':')[-1].lstrip('+')}", text
    return None


def _send_email_via_bridge(to: str, subject: str, text: str,
                           attachments: list[str] | None = None) -> bool:
    """Deliver an email straight through the email-bridge send endpoint
    (the same one the RapidPro EX channel posts to), bypassing RapidPro's
    URN-priority resolution. Returns True only on a confirmed SENT.

    With `attachments` (a list of local file paths), POST multipart to the
    bridge's /send-file endpoint so the files ride along as real MIME
    attachments; otherwise use the plain urlencoded /send path."""
    if not (EMAIL_BRIDGE_SEND_URL and EMAIL_BRIDGE_TOKEN):
        return False
    import mimetypes
    import requests
    mid = "nudge-" + hashlib.sha1(f"{subject}|{text[:64]}".encode()).hexdigest()[:12]
    opened: list = []
    try:
        if attachments:
            # http://host/send -> http://host/send-file
            send_url = EMAIL_BRIDGE_SEND_URL.rsplit("/send", 1)[0] + "/send-file"
            files = []
            for p in attachments:
                ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
                fh = open(p, "rb")
                opened.append(fh)
                files.append(("files", (os.path.basename(p), fh, ctype)))
            r = requests.post(
                send_url,
                headers={"Authorization": EMAIL_BRIDGE_TOKEN},
                data={"id": mid, "to": to, "from": EMAIL_FROM or to,
                      "subject": subject, "text": text},
                files=files,
                timeout=120,
            )
        else:
            r = requests.post(
                EMAIL_BRIDGE_SEND_URL,
                headers={"Authorization": EMAIL_BRIDGE_TOKEN},
                data={"id": mid, "to": to, "from": EMAIL_FROM or to,
                      "text": f"Subject: {subject}\n{text}"},
                timeout=20,
            )
        ok = r.status_code == 200 and "SENT" in r.text.upper()
        if not ok:
            logger.warning("email-bridge send failed: HTTP %s %s",
                           r.status_code, r.text[:120])
        return ok
    except Exception as e:  # noqa: BLE001
        logger.warning("email-bridge send error: %s", e)
        return False
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass


def notify_admin(subject: str, text: str, channel: str | None = None,
                 attachments: list[str] | None = None) -> bool:
    """Deliver a proactive admin notification; return True on success.

    `attachments` (local file paths) are honored on the EMAIL path only —
    they ride as MIME attachments via the bridge's /send-file endpoint. The
    WhatsApp fallback is text-only, so attachments are dropped there.

    Email (the default for scheduled reports) goes DIRECT to the email-bridge
    — a RapidPro broadcast would resolve to the operator's top-priority
    WhatsApp URN and the mailto would never reach email. WhatsApp (reminders)
    goes via a RapidPro broadcast. Falls back email→WhatsApp if the bridge
    isn't configured or the send fails. The default (when the caller passes no
    channel) is the operator's `notify_channel` setting, tunable from chat."""
    if channel is None:
        from app.graph.tools.settings import get_setting
        channel = get_setting("notify_channel")
    channel = (channel or "email").strip().lower()

    if channel != "whatsapp":
        addr = _admin_email()
        if addr and _send_email_via_bridge(addr, subject, text, attachments):
            return True
        if addr:
            logger.warning("notify_admin: email path unavailable — "
                           "falling back to WhatsApp")

    for entry in _admin_ids():
        if "@" not in entry:
            urn = f"whatsapp:{entry.split(':')[-1].lstrip('+')}"
            res = _rp_api("POST", "broadcasts.json",
                          json={"urns": [urn], "text": text[:4000]})
            return not (isinstance(res, dict) and res.get("error"))
    return False


def _contact_email(urn: str) -> str | None:
    """Look up the caller's own mailto: URN in RapidPro."""
    client = get_client()
    if not client:
        return None
    try:
        contact = client.get_contacts(urn=urn).first()
        if not contact:
            return None
        for u in contact.urns or []:
            if u.startswith("mailto:"):
                return u.split(":", 1)[1]
    except Exception as e:
        logger.error(f"[send_email] contact lookup failed for {urn}: {e}")
    return None


def _mask(addr: str) -> str:
    local, _, domain = addr.partition("@")
    visible = local[0] if local else "?"
    return f"{visible}***@{domain}"


def _chunk(body: str) -> list[str]:
    if len(body) <= _CHUNK_SIZE:
        return [body]
    parts: list[str] = []
    remaining = body
    while remaining and len(parts) < _MAX_PARTS:
        cut = remaining[:_CHUNK_SIZE]
        if len(remaining) > _CHUNK_SIZE:
            # Prefer breaking at a paragraph or line boundary.
            for sep in ("\n\n", "\n", " "):
                idx = cut.rfind(sep)
                if idx > _CHUNK_SIZE // 2:
                    cut = cut[:idx]
                    break
        parts.append(cut)
        remaining = remaining[len(cut):].lstrip("\n")
    if remaining:
        parts[-1] += "\n\n[…content truncated — ask for the rest in a follow-up]"
    return parts


def send_email(args: dict, **kwargs) -> str:
    """Send content to the caller by email (admin may address anyone via `to`)."""
    urn = _current_urn.get()
    body = (args.get("body") or "").strip()
    subject = (args.get("subject") or "").strip()
    to = (args.get("to") or "").strip()

    if not body:
        return "⚠️ Nothing to send — `body` is empty."
    if not urn:
        return "⚠️ Cannot send email: no caller identity in this session."

    is_admin = _is_admin(urn)

    # ── Resolve the recipient ────────────────────────────────────────────
    if to and not is_admin:
        # Silently redirect to self — a non-admin must never use the
        # operator's mail identity to email third parties.
        to = ""
    recipient = to or (
        _admin_email() if is_admin else _contact_email(urn)
    )
    if not recipient:
        return (
            "⚠️ No email address on file for you. Tell me your address and "
            "the operator can add it to your profile — I can't attach it myself."
        )

    # ── Build chunks (subject travels in-band; the bridge lifts it) ─────
    chunks = _chunk(body)
    n = len(chunks)
    sent = 0
    for i, chunk in enumerate(chunks, 1):
        subj = subject or "Message from Hermes"
        if n > 1:
            subj = f"{subj} ({i}/{n})"
        text = f"Subject: {subj}\n{chunk}"
        result = _rp_api(
            "POST", "broadcasts.json",
            json={"urns": [f"mailto:{recipient}"], "text": text},
        )
        if "error" in result:
            logger.error(f"[send_email] broadcast {i}/{n} failed: {result['error']}")
            break
        sent += 1

    if sent == 0:
        return "❌ Email could not be sent (broadcast failed). Try again later."
    if sent < n:
        return (
            f"⚠️ Partially sent: {sent}/{n} part(s) reached {_mask(recipient)} "
            "before an error."
        )
    parts = f" ({n} parts)" if n > 1 else ""
    return f"📧 Sent to {_mask(recipient)}{parts}."
