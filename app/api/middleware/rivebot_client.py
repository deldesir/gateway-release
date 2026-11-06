"""
RiveBot HTTP client — called from the AI Gateway to:
  1. Pre-match messages before Hermes Agent (match_intent)
"""

import os
from typing import Optional

import httpx
from loguru import logger

RIVEBOT_URL = os.getenv("RIVEBOT_URL", "http://127.0.0.1:8087")

async def match_intent(
    message: str, persona: str, user_id: str = "user"
) -> tuple[Optional[str], dict]:
    """
    Call RiveBot to attempt a deterministic match before invoking Hermes Agent.

    Returns:
        (response, context) where:
        - response: the reply string if matched, or None to fall through to AI
        - context:  session context dict from RiveBot (lang, topic, history)
                    Always returned, even on fallback, for AI continuity.
                    May contain "silent": True when noai 3rd+ fallback triggers.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{RIVEBOT_URL}/match",
                json={"message": message, "persona": persona, "user": user_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                context = data.get("context", {})
                # Propagate silence flag from noai escalation
                if data.get("silent"):
                    context["silent"] = True
                if data.get("matched"):
                    return data.get("response"), context
                return None, context
        return None, {}
    except httpx.TimeoutException:
        logger.warning(f"[rivebot] Timeout reaching {RIVEBOT_URL}")
        return None, {}
    except Exception as e:
        logger.error(f"[rivebot] Error calling brain service: {e}")
        return None, {}


async def set_var(
    persona: str, user_id: str, var: str, value: str
) -> None:
    """Set a RiveScript user variable via RiveBot's /set-var endpoint.

    Fire-and-forget — never blocks the response pipeline.
    Used to toggle noai mode when AI fails or recovers.
    """
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.post(
                f"{RIVEBOT_URL}/set-var",
                json={"persona": persona, "user": user_id, "var": var, "value": value},
            )
            if resp.status_code == 200:
                _msg = "[rivebot] " + persona + ":" + user_id + " — set " + var + "=" + value
                logger.opt(depth=0).info(_msg.replace("{", "{{").replace("}", "}}"))
            else:
                logger.warning(f"[rivebot] set-var returned {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        logger.warning(f"[rivebot] set-var failed (non-blocking): {e}")


async def set_vars(
    persona: str, user_id: str, variables: dict[str, str]
) -> None:
    """Batch-set multiple RiveScript user variables in one call.

    Single HTTP round-trip instead of N sequential /set-var calls.
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                f"{RIVEBOT_URL}/set-vars",
                json={"persona": persona, "user": user_id, "vars": variables},
            )
            if resp.status_code == 200:
                keys = ", ".join(k + "=" + v for k, v in variables.items())
                _msg = "[rivebot] " + persona + ":" + user_id + " — set " + keys
                logger.opt(depth=0).info(_msg.replace("{", "{{").replace("}", "}}"))
            else:
                logger.warning(f"[rivebot] set-vars returned {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        logger.warning(f"[rivebot] set-vars failed (non-blocking): {e}")
