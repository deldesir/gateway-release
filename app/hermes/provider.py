"""Which native Hermes provider the gateway runs on, and what keeps it robust.

One place answers: the pinned provider (``HERMES_PROVIDER``, or Gemini when only a Gemini
key is set), the key that belongs to it, the model for a persona, the fallback chain Hermes
switches to when the primary fails mid-turn (rate limit, credit exhausted, auth, overload,
timeout), the effort level, and a token tally the health page shows so credit burn is visible.

Env:
    HERMES_PROVIDER         gemini | anthropic | "" (OpenAI-compatible endpoint)
    GEMINI_API_KEY / GOOGLE_API_KEY, ANTHROPIC_API_KEY
    LLM_MODEL               the model for every persona without an override
    LLM_MODEL_<PERSONA>     the model for one persona (slug upper-cased, "-" as "_"),
                            e.g. LLM_MODEL_TALKPREP
    LLM_FALLBACK_CHAIN      "provider:model, provider:model, ..."; "none" disables.
                            Default: one rung down on the same provider (Fable → Opus 5.5 →
                            Sonnet 5.5 → Haiku 5.5), then the other provider when its key
                            is set
    LLM_FALLBACK_PROVIDER / LLM_FALLBACK_MODEL
                            the single-entry form of the chain; "none" disables
    HERMES_EFFORT           low | medium | high | xhigh | max (unset: Hermes' default, medium)
"""

import os
import re
import threading
import time

PROVIDER_KEY_VARS = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
}
# The cross-provider rung: the cheap model of the other provider.
FALLBACK_DEFAULT_MODEL = {
    "gemini": "gemini-3.5-flash",
    "anthropic": "claude-haiku-5-5",
}
# The same-provider rung, one step down from the primary model (matched by prefix).
SAME_PROVIDER_LADDER = {
    "anthropic": (
        ("claude-fable", "claude-opus-5-5"),
        ("claude-opus", "claude-sonnet-5-5"),
        ("claude-sonnet", "claude-haiku-5-5"),
    ),
}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_NOT_A_PERSONA = {"SUMMARIZE"}  # LLM_MODEL_SUMMARIZE is an older setting, not a persona


def provider_key(provider: str) -> str:
    """The API key configured for a native provider, or ""."""
    for var in PROVIDER_KEY_VARS.get(provider, ()):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return ""


def active_provider() -> str:
    """The pinned provider; a Gemini key alone selects Gemini (LiteLLM is retired, so the
    OpenAI-compatible path is only ever chosen on purpose, by leaving HERMES_PROVIDER unset
    with no Gemini key)."""
    pinned = os.getenv("HERMES_PROVIDER", "").strip().lower()
    if pinned:
        return pinned
    if provider_key("gemini"):
        return "gemini"
    return ""


def _persona_var(persona: str) -> str:
    return "LLM_MODEL_" + re.sub(r"[^A-Z0-9]", "_", persona.strip().upper())


def _setting(key: str):
    """A value from the operator's settings store (settings.json, then the legacy env
    variable, then the schema default); None when the store is unavailable."""
    try:
        from app.graph.tools.settings import get_setting
        return get_setting(key)
    except Exception:
        return None


def model_for(persona: str | None = None) -> str:
    """The model a persona runs on: the operator's setting for it, else its env override,
    else the line's model (setting, then LLM_MODEL)."""
    if persona:
        own = _setting(f"llm_model_{re.sub(r'[^a-z0-9]', '_', persona.strip().lower())}")
        if own:
            return str(own)
        override = os.getenv(_persona_var(persona), "").strip()
        if override:
            return override
    line = _setting("llm_model")
    return str(line) if line else os.getenv("LLM_MODEL", "").strip()


def persona_models() -> dict[str, str]:
    """Every per-persona override present in the environment, for the health page."""
    out = {}
    for var, value in os.environ.items():
        if var.startswith("LLM_MODEL_") and value.strip():
            name = var[len("LLM_MODEL_"):]
            if name and name not in _NOT_A_PERSONA:
                out[name.lower().replace("_", "-")] = value.strip()
    return dict(sorted(out.items()))


def _parse_chain(spec: str) -> list[dict]:
    entries = []
    for item in spec.split(","):
        provider, _, model = item.strip().partition(":")
        provider, model = provider.strip().lower(), model.strip()
        if provider and model:
            entries.append({"provider": provider, "model": model})
    return entries


def same_provider_rung(provider: str, model: str) -> str:
    """One step down on the same provider, or "" when the ladder has no rung below."""
    for prefix, rung in SAME_PROVIDER_LADDER.get(provider, ()):
        if (model or "").startswith(prefix):
            return rung
    return ""


def fallback_chain(primary: str, model: str = "") -> list[dict]:
    """The Hermes fallback chain for a primary provider and model. Entries whose provider has
    no key are dropped, as is the primary model itself; "none" switches the chain off."""
    spec = os.getenv("LLM_FALLBACK_CHAIN", "").strip()
    single = os.getenv("LLM_FALLBACK_PROVIDER", "").strip().lower()
    if spec.lower() == "none" or single == "none" or _setting("llm_fallback") == "none":
        return []
    if spec:
        chain = _parse_chain(spec)
    elif single:
        single_model = os.getenv("LLM_FALLBACK_MODEL", "").strip() or FALLBACK_DEFAULT_MODEL.get(single, "")
        chain = [{"provider": single, "model": single_model}] if single_model else []
    else:
        chain = []
        rung = same_provider_rung(primary, model)
        if rung:
            chain.append({"provider": primary, "model": rung})
        for other in PROVIDER_KEY_VARS:
            if other != primary and FALLBACK_DEFAULT_MODEL.get(other):
                chain.append({"provider": other, "model": FALLBACK_DEFAULT_MODEL[other]})
    out: list[dict] = []
    for entry in chain:
        if not provider_key(entry["provider"]):
            continue
        if entry["provider"] == primary and entry["model"] == (model or "").strip():
            continue
        if entry not in out:
            out.append(entry)
    return out


def reasoning_config() -> dict | None:
    """``{"effort": ...}`` when HERMES_EFFORT names a level; None leaves Hermes' default."""
    effort = str(_setting("llm_effort") or os.getenv("HERMES_EFFORT", "")).strip().lower()
    return {"effort": effort} if effort in EFFORT_LEVELS else None


def agent_provider_kwargs(provider: str | None = None, model: str | None = None) -> dict:
    """The AIAgent keyword arguments that select the provider, its key, its fallback chain
    and the effort. Without a native provider, the OpenAI-compatible endpoint settings."""
    provider = active_provider() if provider is None else provider
    model = model_for() if model is None else model
    if not provider:
        return {
            "api_key": os.getenv("OPENAI_API_KEY", ""),
            "base_url": os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1"),
        }
    kwargs: dict = {"provider": provider}
    key = provider_key(provider)
    if key:
        kwargs["api_key"] = key  # other providers resolve their own key from the environment
    chain = fallback_chain(provider, model)
    if chain:
        kwargs["fallback_model"] = chain
    effort = reasoning_config()
    if effort:
        kwargs["reasoning_config"] = effort
    return kwargs


# ── Token tally since the process started (the health page shows it) ───────────────────
_usage_lock = threading.Lock()
_usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0, "since": time.time()}


def record_usage(prompt_tokens: int, completion_tokens: int) -> None:
    with _usage_lock:
        _usage["turns"] += 1
        _usage["prompt_tokens"] += int(prompt_tokens or 0)
        _usage["completion_tokens"] += int(completion_tokens or 0)


def usage_snapshot() -> dict:
    with _usage_lock:
        return {
            "turns": _usage["turns"],
            "prompt_tokens": _usage["prompt_tokens"],
            "completion_tokens": _usage["completion_tokens"],
            "since_seconds": int(time.time() - _usage["since"]),
        }


def reset_usage() -> None:
    """For tests."""
    with _usage_lock:
        _usage.update({"turns": 0, "prompt_tokens": 0, "completion_tokens": 0, "since": time.time()})
