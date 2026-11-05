"""Golden Set scenario loader — reads Social-Code's multilingual scenario bank.

Loads the 58 scenarios (4 levels × 4 languages) from data/social-code/scenarios/.
Each scenario includes context, cue, ideal links, and metadata.
"""

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "social-code" / "scenarios"

# In-memory cache (loaded once at startup)
_scenarios: Dict[int, List[Dict]] = {}


def _load_from_disk() -> Dict[int, List[Dict]]:
    """Load all scenario JSON files from disk, keyed by difficulty level."""
    result: Dict[int, List[Dict]] = {}
    if not _DATA_DIR.exists():
        return result

    for fpath in sorted(_DATA_DIR.glob("*.json")):
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            for item in data:
                level = item.get("difficulty", 1)
                result.setdefault(level, []).append(item)
        except (json.JSONDecodeError, OSError):
            pass

    return result


def load_scenarios() -> Dict[int, List[Dict]]:
    """Return the cached scenario bank, loading from disk on first call."""
    global _scenarios
    if not _scenarios:
        _scenarios = _load_from_disk()
    return _scenarios


def get_scenario(
    difficulty: int = 1,
    lang: str = "en",
    exclude_ids: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch a random scenario at the given difficulty level.

    Returns a localized scenario dict with keys:
        context, cue, cue_category, target_persona, cultural_context, ideal_links

    Returns None if no scenarios exist at that level.
    """
    bank = load_scenarios()
    pool = bank.get(difficulty, [])

    if not pool:
        # Fall back to any available level
        for lvl in sorted(bank.keys()):
            if bank[lvl]:
                pool = bank[lvl]
                break
    if not pool:
        return None

    # Exclude already-seen scenarios in this session
    if exclude_ids:
        available = [s for i, s in enumerate(pool) if i not in exclude_ids]
        if not available:
            available = pool  # Wrap around if all seen
    else:
        available = pool

    scenario = random.choice(available)

    # Localize — scenario fields can be either str or {lang: str} dicts
    def _localize(field):
        if isinstance(field, dict):
            return field.get(lang, field.get("en", str(field)))
        return field

    localized = {
        "context": _localize(scenario.get("context", "")),
        "cue": _localize(scenario.get("cue", "")),
        "cue_category": scenario.get("cue_category", "General"),
        "difficulty": scenario.get("difficulty", 1),
        "target_persona": scenario.get("target_persona", "Stranger"),
        "cultural_context": scenario.get("cultural_context", ""),
        "tags": scenario.get("tags", []),
    }

    # Localize ideal links (for offline grading reference)
    ideal_links = []
    for link in scenario.get("ideal_links", []):
        ideal_links.append({
            "angle_type": link.get("angle_type", ""),
            "link_text": _localize(link.get("link_text", "")),
            "explanation": _localize(link.get("explanation", "")),
        })
    localized["ideal_links"] = ideal_links

    return localized


def format_scenario_whatsapp(scenario: Dict[str, Any]) -> str:
    """Format a scenario for WhatsApp display."""
    ctx = scenario.get("context", "")
    cue = scenario.get("cue", "")
    persona = scenario.get("target_persona", "Someone")
    difficulty = scenario.get("difficulty", 1)
    level_emoji = {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴"}.get(difficulty, "⚪")

    return (
        f"{level_emoji} *Level {difficulty}* — {persona}\n\n"
        f"📍 *Scene:* {ctx}\n\n"
        f"💬 *Cue:* _{cue}_\n\n"
        f"How would you respond?"
    )
