"""
Health check endpoint — reports the active LLM path and the checkpointer DB.

The gateway runs Hermes with the native Gemini provider (LiteLLM is retired).
The probe is provider-aware: it checks configuration of whatever path is
actually active rather than unconditionally pinging the old LiteLLM proxy on
:4000 — pinging a retired service would always report "degraded".

Returns HTTP 200 when all services are operational, 503 when degraded.
"""

import importlib.metadata
import os

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["observability"])


import time

from app.api.middleware import circuit_breaker
from app.hermes.provider import active_provider, fallback_chain, model_for, persona_models, usage_snapshot

NATIVE_PROVIDERS = ("gemini", "anthropic")


def _active_provider() -> str:
    """The provider the Hermes engine will use (one resolution, in app/hermes/provider.py):
    ``HERMES_PROVIDER``, else Gemini when its key is set, else the OpenAI-compatible endpoint."""
    return active_provider() or "openai-compat"


# The Anthropic probe lists models: a request that costs no tokens and still fails on a bad
# key, an exhausted organization or an unreachable API. Cached so a monitor polling /health
# does not hammer the API; failures are re-checked sooner than successes.
_ANTHROPIC_PROBE_TTL_OK = 600.0
_ANTHROPIC_PROBE_TTL_BAD = 60.0
_anthropic_probe: dict = {"at": 0.0, "status": ""}


async def _anthropic_models_status(key: str) -> int:
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            "https://api.anthropic.com/v1/models?limit=1",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
    return r.status_code


async def _probe_anthropic() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return "no api key"
    now = time.time()
    cached = _anthropic_probe["status"]
    ttl = _ANTHROPIC_PROBE_TTL_OK if cached == "ok" else _ANTHROPIC_PROBE_TTL_BAD
    if cached and now - _anthropic_probe["at"] < ttl:
        return cached
    try:
        code = await _anthropic_models_status(key)
        if code == 200:
            status = "ok"
        elif code in (401, 403):
            status = "auth error"
        else:
            status = f"degraded ({code})"
    except Exception:
        status = "unreachable"
    _anthropic_probe.update(at=now, status=status)
    return status


async def _probe_openai_compat() -> str:
    """Probe the configured OpenAI-compatible endpoint (LiteLLM / custom)."""
    base = os.getenv("OPENAI_API_BASE", "http://localhost:4000").rstrip("/")
    key = os.getenv("OPENAI_API_KEY", "")
    try:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{base}/health", headers=headers)
        return "ok" if r.status_code == 200 else f"degraded ({r.status_code})"
    except Exception:
        return "unreachable"


async def _probe_llm() -> str:
    """Report the active LLM path.

    For the native Gemini provider, a liveness probe must not spend tokens, so
    we check that a key is configured rather than calling the API. For the
    OpenAI-compatible fallback we still ping the configured endpoint.
    """
    provider = _active_provider()
    if provider == "gemini":
        has_key = bool(
            os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        )
        return "ok" if has_key else "no api key"
    if provider == "anthropic":
        return await _probe_anthropic()
    if provider == "openai-compat":
        return await _probe_openai_compat()
    # Some other explicitly-pinned native provider — config presence only.
    return "ok"


from sqlalchemy import text
from app.db import async_session

async def _probe_db() -> str:
    """Check if the main DB is accessible."""
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:
        return f"error: {e}"


@router.get("/health")
async def health() -> JSONResponse:
    """Structured health check — reports the active LLM path and the DB.

    Returns:
        200 when operational, 503 if any critical service is down.
    """
    try:
        version = importlib.metadata.version("ai-gateway")
    except Exception:
        version = "dev"

    provider = _active_provider()
    llm_status, db_status = await _probe_llm(), await _probe_db()
    is_ok = llm_status == "ok" and db_status == "ok"

    # Every auth gate falls open in "dev mode" when its env var is unset —
    # convenient in development, silent exposure in production. Surface it
    # here so a config regression (e.g. a bad role re-run) is visible.
    open_gates = [
        name for name, var in (
            ("authorized_users", "AUTHORIZED_USERS"),
            ("gateway_api_key", "GATEWAY_API_KEY"),
            ("gateway_internal_key", "GATEWAY_INTERNAL_KEY"),
        ) if not os.getenv(var, "").strip()
    ]

    # The breaker opens after repeated AI failures; while it is open every caller gets
    # the "temporarily unavailable" line, so the page says so and the monitor sees 503.
    circuit = circuit_breaker.status()
    is_ok = is_ok and circuit.get("state") != "open"

    content = {
        "status": "ok" if is_ok else "degraded",
        "version": version,
        "services": {"llm": llm_status, "provider": provider, "db": db_status},
        "llm": {
            "model": model_for(),
            "per_persona": persona_models(),
            "fallback": fallback_chain(provider, model_for()) if provider in NATIVE_PROVIDERS else [],
            "circuit": circuit,
            "usage_since_start": usage_snapshot(),
        },
    }
    if open_gates:
        content["status"] = "degraded" if is_ok else content["status"]
        content["auth_gates_open"] = open_gates

    return JSONResponse(status_code=200 if is_ok else 503, content=content)
