import json
import re
from typing import Dict, Any, Optional

def clean_json_response(response: str) -> Dict[str, Any]:
    """
    Cleans and parses a JSON response from an LLM, handling markdown code blocks
    and common syntax errors.

    Args:
        response (str): The raw string response from the LLM.

    Returns:
        Dict[str, Any]: The parsed JSON object, or an empty dict if parsing fails.
    """
    # Remove markdown code blocks (```json ... ```)
    if "```" in response:
        # Extract content between the first and last triple backticks
        match = re.search(r"```(?:json)?(.*?)```", response, re.DOTALL)
        if match:
            response = match.group(1)

    # Trim whitespace
    response = response.strip()

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        # Fallback: Try to find the first '{' and last '}'
        start = response.find("{")
        end = response.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(response[start : end + 1])
            except json.JSONDecodeError:
                pass
        
        return {}
