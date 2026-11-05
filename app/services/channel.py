"""
Channel → Persona routing.

Resolution order:
  1. ChannelConfig DB table (phone → persona_id)
  2. Persona slug direct match (e.g., "konex-support")
  3. DEFAULT_PERSONA environment variable
"""

import os
from sqlmodel import select
from app.db import get_session
from app.models import ChannelConfig, Persona
from app.logger import logger

api_logger = logger.bind(name="ChannelService")

# Default MUST be an unprivileged persona: every unmapped caller and the
# tools-API fallback inherit it. "assistant" here would hand strangers the
# operator toolsets (terminal, config, system) on any deploy missing the env.
DEFAULT_PERSONA = os.getenv("DEFAULT_PERSONA", "general")


async def resolve_persona(channel_or_slug: str) -> tuple[str, str | None]:
    """
    Resolve the configured Persona slug and optional System Prompt Override
    for a given channel phone number or persona slug.

    Returns:
        (persona_slug, system_prompt_override)
    """
    try:
        async for session in get_session():
            # 1. Try ChannelConfig match (phone → persona)
            result = await session.exec(
                select(ChannelConfig).where(
                    ChannelConfig.channel_phone == channel_or_slug
                )
            )
            channel_config = result.first()

            if channel_config:
                # Found config — resolve the mapped persona
                persona = await session.get(Persona, channel_config.persona_id)
                if persona:
                    api_logger.info(
                        f"Channel '{channel_or_slug}' → Persona '{persona.slug}'"
                    )
                    return persona.slug, channel_config.system_prompt_override
                else:
                    api_logger.warning(
                        f"Channel '{channel_or_slug}' maps to missing "
                        f"persona_id '{channel_config.persona_id}'"
                    )

            # 2. Try Persona slug direct match
            result = await session.exec(
                select(Persona).where(Persona.slug == channel_or_slug)
            )
            persona = result.first()
            if persona:
                return persona.slug, None

            break
    except Exception as e:
        api_logger.error(f"Error resolving persona: {e}")

    # 3. Fallback to DEFAULT_PERSONA
    api_logger.info(
        f"No mapping for '{channel_or_slug}', using default: {DEFAULT_PERSONA}"
    )
    return DEFAULT_PERSONA, None
