"""
API router — thin wiring layer.

Mounts all sub-routers. No business logic lives here.
To add a new endpoint group, create a module and include its router below.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from app.api.schemas import ChatRequest, ChatResponse
from app.api.health import router as health_router
from app.api.adapters.openai import router as openai_router
from app.api.adapters.tools import router as tools_router
from app.api.routers.plugins import router as plugins_router

# Main router — all sub-routers are mounted with no prefix (app.py handles prefixes)
router = APIRouter()
router.include_router(health_router)
router.include_router(openai_router)
router.include_router(tools_router)
router.include_router(plugins_router)


# ── Legacy endpoint (kept for backwards compat) ───────────────────────────────

@router.post("/chat/", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> ChatResponse:
    """Legacy HTTP chat endpoint — superseded by /v1/chat/completions."""
    if x_user_id is None:
        raise HTTPException(status_code=400, detail="X-User-Id header is required")

    session_id = payload.session_id or str(uuid.uuid4())
    # TODO: implement full legacy flow if needed
    return ChatResponse(
        response="Use /v1/chat/completions instead.",
        session_id=session_id,
        persona=payload.persona,
    )
