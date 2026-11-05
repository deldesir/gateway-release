"""Signed download links for generated files (Anki decks and the like).

A tool writes a file under /tmp with the jwlinker_ prefix and hands the user a link over
WhatsApp. The link carries an expiry and a signature over the file name and that expiry,
made with the gateway's API key. Without a valid signature the route serves nothing, so
a link cannot be guessed, the file name cannot be swapped, and an old link stops working.
"""

import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter(tags=["downloads"])

DOWNLOADS_DIR = Path("/tmp")
# Only serve files with these extensions
ALLOWED_EXTENSIONS = {".apkg", ".md", ".txt", ".pdf", ".csv"}
# Only serve files with this prefix
ALLOWED_PREFIX = "jwlinker_"
# How long a link stays valid
DEFAULT_TTL = 24 * 3600


def _secret() -> bytes:
    return os.getenv("GATEWAY_API_KEY", "").encode("utf-8")


def _signature(filename: str, expires: int, secret: bytes) -> str:
    return hmac.new(secret, f"{filename}\n{expires}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def download_url(filename: str, ttl: int = DEFAULT_TTL) -> str:
    """The link a tool hands the user: the public base, the file, its expiry and its signature."""
    secret = _secret()
    if not secret:
        raise RuntimeError("GATEWAY_API_KEY is not set; download links cannot be signed")
    base = os.getenv("GATEWAY_PUBLIC_URL", "http://localhost:8086").rstrip("/")
    expires = int(time.time()) + ttl
    return f"{base}/downloads/{filename}?" + urlencode({"e": expires, "t": _signature(filename, expires, secret)})


@router.get("/downloads/{filename}")
async def download_file(
    filename: str,
    e: int = Query(..., description="expiry, seconds since the epoch"),
    t: str = Query(..., description="signature"),
):
    """Serve a generated file, given a link made by download_url()."""
    # No path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=403, detail=f"File type {ext} not allowed")
    if not filename.startswith(ALLOWED_PREFIX):
        raise HTTPException(status_code=403, detail="File not available for download")

    secret = _secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Downloads are not configured")
    # The signature is checked before the file is looked for, so a bad link learns nothing.
    if not hmac.compare_digest(_signature(filename, e, secret), t):
        raise HTTPException(status_code=403, detail="Invalid link")
    if e < int(time.time()):
        raise HTTPException(status_code=403, detail="Link expired; ask for the file again")

    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type="application/octet-stream",
    )
