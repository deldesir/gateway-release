"""Plugin auto-discovery framework.

On import, scans app/plugins/*/tools.py for @register_tool decorated functions
and registers them in the Hermes tool registry.
"""

import importlib
from pathlib import Path

PLUGIN_REGISTRY: dict[str, dict] = {}

def register_tool(
    name: str,
    description: str,
    trigger: str,
    near_miss: list[str] = None,
    admin_only: bool = False,
):
    """Decorator to auto-register a tool across the Gateway and RiveBot.
    
    Args:
        name: Tool function name (e.g. "macro_sys_health")
        description: Human-readable description for LLM schema
        trigger: Exact RiveScript trigger (e.g. "sysadmin health")
        near_miss: Fuzzy match patterns (e.g. ["[*] health [*]"])
        admin_only: Whether this tool requires RBAC authorization
    """
    def wrapper(func):
        PLUGIN_REGISTRY[name] = {
            "name": name,
            "description": description,
            "admin_only": admin_only,
            "trigger": trigger,
            "near_miss": near_miss or [],
            "handler": func,
        }
        return func
    return wrapper


def discover_plugins():
    """Scan app/plugins/*/tools.py and import them to trigger @register_tool."""
    import logging
    _log = logging.getLogger(__name__)
    plugins_dir = Path(__file__).parent
    for item in plugins_dir.iterdir():
        if item.is_dir() and (item / "tools.py").exists():
            module_name = f"app.plugins.{item.name}.tools"
            try:
                importlib.import_module(module_name)
                _log.info(f"Loaded plugin: {module_name}")
            except Exception as e:
                _log.error(f"Failed to load plugin {module_name}: {e}")


# ── Eagerly discover on first import ─────────────────────────────────────────
# This runs when the Gateway process starts and any module imports app.plugins.
# The plugins router imports PLUGIN_REGISTRY from here, so discovery must happen
# before the first request hits the manifest endpoint.
discover_plugins()
