#!/usr/bin/env python
"""Generate the printable contact-sync QR code.

The QR encodes a click-to-chat link that sends the word `menu` to the directory line:

    https://wa.me/<directory line number>?text=menu

**It is not a credential.** It carries no contact data and no token, so whoever
photographs it gains nothing: the request is authorized by the *sender's own*
WhatsApp account, which must be in a `Team: <Name>` RapidPro group. That is why
one poster can serve every team — the team is resolved from the sender.

    uv run python scripts/make_contact_qr.py                 # write PNG + SVG to /tmp
    uv run python scripts/make_contact_qr.py --mailto        # QR for the email channel
    uv run python scripts/make_contact_qr.py --send          # email it to the operator

The number comes from CONTACT_SYNC_WA_NUMBER, else from the directory line's
channel in RapidPro (CONTACT_SYNC_WUZAPI_CHANNEL, in the directory's own
workspace when CONTACT_SYNC_RAPIDPRO_TOKEN selects one), else the newest
WhatsApp channel — never hardcoded, because this repo is public.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEYWORD = os.getenv("CONTACT_SYNC_KEYWORD", "menu")


def _mask(value: str) -> str:
    if "@" in value:
        return value[0] + "***@" + value.split("@", 1)[1]
    return value[:4] + "***" + value[-3:] if len(value) > 7 else "***"


def wa_number() -> str:
    """Box WhatsApp number, digits only."""
    explicit = os.getenv("CONTACT_SYNC_WA_NUMBER", "")
    if explicit:
        return re.sub(r"\D", "", explicit)

    from app.graph.tools.contact_sync import DIRECTORY_CHANNEL, _RP_AUTH
    from app.graph.tools.rapidpro import _rp_api

    pin = DIRECTORY_CHANNEL.lstrip("+")
    channels = [c for c in (_rp_api("GET", "channels.json", **_RP_AUTH).get("results") or [])
                if (c.get("type") or "").lower() in ("wuzapi", "wz")]
    if pin:   # the directory line, by UUID or number
        channels = [c for c in channels if c.get("uuid") == pin or re.sub(r"\D", "", c.get("address") or "") == re.sub(r"\D", "", pin)]
    for channel in channels:
        return re.sub(r"\D", "", channel.get("address") or "")
    return ""


def bridge_address() -> str:
    """Inbound address of the email channel, for the mailto variant."""
    explicit = os.getenv("CONTACT_SYNC_EMAIL_ADDRESS", "")
    if explicit:
        return explicit.strip()

    from app.graph.tools.rapidpro import _rp_api

    data = _rp_api("GET", "channels.json")
    for channel in data.get("results") or []:
        if (channel.get("type") or "").lower() in ("external", "ex"):
            return (channel.get("address") or "").strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp", help="output directory (default /tmp)")
    parser.add_argument("--mailto", action="store_true",
                        help="encode the email channel instead of WhatsApp")
    parser.add_argument("--send", action="store_true",
                        help="email the PNG to the operator")
    args = parser.parse_args()

    if args.mailto:
        address = bridge_address()
        if not address:
            print("No email channel address found. Set CONTACT_SYNC_EMAIL_ADDRESS.", file=sys.stderr)
            return 1
        url = f"mailto:{address}?subject={KEYWORD}"
        stem, channel = "contact-sync-qr-email", "email"
        shown = _mask(address)
    else:
        number = wa_number()
        if not number:
            print("No wuzapi channel found. Set CONTACT_SYNC_WA_NUMBER.", file=sys.stderr)
            return 1
        url = f"https://wa.me/{number}?text={KEYWORD}"
        stem, channel = "contact-sync-qr-whatsapp", "whatsapp"
        shown = _mask("+" + number)

    import segno

    qr = segno.make(url, error="m")
    png = os.path.join(args.out, f"{stem}.png")
    svg = os.path.join(args.out, f"{stem}.svg")
    qr.save(png, scale=10, border=3)
    qr.save(svg, scale=10, border=3)

    print(f"channel : {channel}")
    print(f"target  : {shown}")
    print(f"keyword : {KEYWORD}")
    print(f"written : {png}")
    print(f"          {svg}")

    if args.send:
        from app.graph.tools.email import notify_admin

        body = (
            f"Printable contact-sync QR ({channel}).\n\n"
            f"Scanning it sends '{KEYWORD}' to the directory line. Anyone may scan it — the QR is\n"
            "not a credential and holds no contact data. Only senders in a 'Team:' or 'Access:'\n"
            "RapidPro group get an answer; everyone else gets silence.\n"
        )
        if notify_admin("Contact sync — printable QR", body, attachments=[png]):
            print("emailed : yes")
        else:
            print("emailed : FAILED", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
