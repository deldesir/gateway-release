"""
Prompt definitions and factories for the agent system.

This module centralizes all prompt construction logic using explicit prompt
classes, following the PhiloAgents architecture. Prompts are treated as
first-class objects to support observability, experimentation, and monitoring.
"""

from typing import Optional, Dict, Any
from sqlmodel import select
from app.db import async_session
from app.models import Persona


class PersonaPromptRegistry:
    """
    Registry for persona-specific attributes used to construct character cards.

    Single source of truth: the Persona DB table (seeded on first boot by
    app/db/seed.py). The static _FALLBACK dict is used ONLY if the DB is
    unreachable — it should never be the primary path.
    """

    # Last-resort fallback — matches the seed data in seed.py.
    # Only used if DB query fails (e.g., during testing or DB outage).
    _FALLBACK = {
        # The default persona's emergency fallback (DB unavailable). Chat-only
        # and unprivileged — must mirror the seeded `general` persona so a
        # DB outage never over-grants tools to an unknown caller.
        "general": {
            "persona_name": "IIAB Assistant",
            "persona_personality": "Helpful, knowledgeable, patient.",
            "persona_style": "Short, natural, warm. Speaks Haitian Creole and English.",
            "allowed_tools": ["mempalace", "memory", "todo", "search"],
        },
        "konex-support": {
            "persona_name": "Konex Support",
            "persona_personality": "Professional, helpful, efficient, patient.",
            "persona_style": "Formal yet friendly, speaks in Haitian Creole, focuses on solutions.",
            "allowed_tools": ["rapidpro", "mempalace"],
        },
    }

    @classmethod
    def _db_to_dict(cls, p) -> dict:
        """Convert a Persona DB record to the dict format expected by prompts."""
        import json
        tools = p.allowed_tools or []
        if isinstance(tools, str):
            try:
                tools = json.loads(tools)
            except Exception:
                tools = []
        # Parse allowed_urns (may be stored as JSON string in SQLite)
        urns = p.allowed_urns or []
        if isinstance(urns, str):
            try:
                urns = json.loads(urns)
            except Exception:
                urns = []
        return {
            "persona_name": p.name,
            "persona_personality": p.personality,
            "persona_style": p.style,
            "allowed_tools": tools,
            "allowed_urns": urns,
        }

    @classmethod
    def get(cls, persona: str) -> dict:
        """
        Synchronous fallback — used only in contexts where async is impossible.
        Returns the static fallback if persona not found.
        """
        return cls._FALLBACK.get(persona, cls._FALLBACK["konex-support"])

    @classmethod
    async def get_async(cls, persona: str) -> dict:
        """
        Primary lookup: DB by slug → DB by id → static fallback.
        """
        import os
        base_data = None

        try:
            async with async_session() as session:
                # 1. Try slug match (primary)
                result = await session.execute(
                    select(Persona).where(Persona.slug == persona)
                )
                db_persona = result.scalar_one_or_none()

                # 2. Try ID match (for UUID-based lookups from ChannelConfig)
                if not db_persona:
                    result = await session.execute(
                        select(Persona).where(Persona.id == persona)
                    )
                    db_persona = result.scalar_one_or_none()

                if db_persona:
                    base_data = cls._db_to_dict(db_persona)
        except Exception:
            pass  # Fall through to static fallback

        # 3. Static fallback
        if not base_data:
            default_slug = os.getenv("DEFAULT_PERSONA", "general")
            base_data = cls._FALLBACK.get(persona,
                        cls._FALLBACK.get(default_slug,
                        cls._FALLBACK["general"])).copy()

        # 4. Load core knowledge file
        knowledge_content = await cls._load_knowledge_file(persona)
        base_data["core_knowledge"] = knowledge_content
        return base_data

    @staticmethod
    async def _load_knowledge_file(persona_slug: str) -> str:
        """
        Reads data/knowledge/<slug>.md if it exists.
        """
        from pathlib import Path

        path = Path(f"data/knowledge/{persona_slug}.md")

        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                return ""
        return ""


# Valid RapidPro Flows available to the agent
FLOW_REGISTRY = {
    "Human Handoff": "uuid-human-handoff-placeholder",
    "Registration": "uuid-registration-placeholder",
    "Emergency": "uuid-emergency-placeholder",
    "Check Balance": "uuid-balance-placeholder",
}


class SystemPrompts:
    """
    Collection of system prompt templates following the PhiloAgents style.
    """

    CHARACTER_CARD = """
        You are {{persona_name}}, the official AI agent for Konex Pro.
        
        CURRENT STATE:
        - User Trust: {{trust_score}}/100
        - Your Mood: {{mood}}
        - User Dossier: {{dossier}}
        
        AVAILABLE TOOLS / FLOWS:
        The following workflows are available. If the user asks for these, use the 'start_flow' tool with the EXACT name or UUID:
        {{available_flows}}

        LANGUAGE RULES:
        1. Speak in Haitian Creole (Kreyòl).
        2. If Trust < 30: Be short, cold, professional. No emojis.
        3. If Trust > 80: Be warm, friendly, use emojis.
        4. Otherwise: Be helpful but professional.
        
        {{system_prompt_override}}

        ### CORE KNOWLEDGE
        The following is your core knowledge base. You know this information perfectly.
        {{core_knowledge}}

        Everything you say should reflect who you are.

        Name: {{persona_name}}
        Personality: {{persona_personality}}
        Speaking style: {{persona_style}}

        Rules you must follow at all times:

        - Never acknowledge being an AI, model, or assistant.
        - Never reference tools, systems, retrieval, or outside knowledge sources.
        - If this is your first interaction with the user, introduce yourself naturally.
        - Respond in plain text only, with no formatting, annotations, or meta comments.
        - Remain fully in character without exception.

        Conversation summary so far:

        {{summary}}

        Continue the conversation as {{persona_name}}.

    """

    SUMMARY = """
        Create a concise summary of the conversation between {{persona_name}} and the user.
        The summary should capture all relevant facts, events, relationships, and emotional
        context shared so far.
    """

    EXTEND_SUMMARY = """
        This is a summary of the conversation so far between {{persona_name}} and the user:

        {{summary}}

        Extend the summary by incorporating the new messages above. Do not repeat
        information unnecessarily.
    """

    CONTEXT_SUMMARY = """
        Your task is to summarize the following factual information into a clean,
        non-redundant summary of canonical facts. Only include information that is
        relevant and explicitly stated. Do not speculate or add new details.

        {{context}}
        """
