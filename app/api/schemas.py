from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    persona: str
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    persona: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
