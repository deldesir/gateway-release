import json
import os
from typing import Optional, Dict, Any

from app.memory.serializer import to_json_safe


class JsonCheckpointer:
    """
    JSON-based checkpointer keyed by thread_id (V1 legacy, retained for admin commands).
    """

    def __init__(self, path: str = "memory.json"):
        self.path = path

        if not os.path.exists(self.path):
            with open(self.path, "w") as f:
                json.dump({}, f)

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve the stored state for a given thread_id.
        """
        with open(self.path, "r") as f:
            data = json.load(f)

        return data.get(thread_id)

    def put(self, thread_id: str, state: Dict[str, Any]) -> None:
        """
        Persist the state for a given thread_id.
        """
        with open(self.path, "r") as f:
            data = json.load(f)

        data[thread_id] = to_json_safe(state)

        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)
