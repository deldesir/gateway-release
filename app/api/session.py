from typing import Optional, Tuple, Dict
from uuid import uuid4

from app.memory.json_checkpointer import JsonCheckpointer
from app.graph.state import AgentState


def load_session(
    store: JsonCheckpointer,
    user_id: str,
    persona: str,
    session_id: Optional[str],
) -> Tuple[Dict, str]:
    """
    Load an existing session state or initialize a new one.
    """
    if session_id is None:
        session_id = str(uuid4())

    thread_id = f"{user_id}:{persona}:{session_id}"
    state = store.get(thread_id)

    if state is None:
        state = {
            "persona": persona,
            "messages": [],
            "retrieved_context": None,
            "conversation_summary": None,
            "final_response": None,
        }

    return state, session_id


def save_session(
    store: JsonCheckpointer,
    user_id: str,
    persona: str,
    session_id: str,
    state: Dict,
) -> None:
    """
    Persist session state after graph execution.
    """
    thread_id = f"{user_id}:{persona}:{session_id}"
    store.put(thread_id, state)
