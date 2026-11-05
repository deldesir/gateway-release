"""OpenAI function-calling schemas for Social-Code simulation tools.

These schemas define the tool interface that Hermes presents to the LLM.
The LLM uses these tools to update simulation state during roleplay.
"""

SIM_UPDATE_MOOD = {
    "name": "sim_update_mood",
    "description": (
        "Update the persona's emotional state during a social simulation. "
        "Call this EVERY turn to reflect how the user's message made you feel."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "new_mood": {
                "type": "string",
                "description": "New emotional state",
                "enum": [
                    "Neutral", "Friendly", "Happy", "Curious",
                    "Annoyed", "Hostile", "Confused", "Suspicious", "Impressed",
                ],
            },
            "internal_thought": {
                "type": "string",
                "description": "Your private thought about the user's message (not shown to user).",
            },
            "reason": {
                "type": "string",
                "description": "Why you changed mood.",
            },
        },
        "required": ["new_mood", "internal_thought", "reason"],
    },
}

SIM_UPDATE_TRUST = {
    "name": "sim_update_trust",
    "description": (
        "Adjust the trust score based on the user's social behavior. "
        "Positive for rapport-building, negative for overstepping."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trust_change": {
                "type": "integer",
                "description": "Change in trust (-10 to +10). Positive = trust gained.",
            },
            "reason": {
                "type": "string",
                "description": "Why trust changed.",
            },
        },
        "required": ["trust_change", "reason"],
    },
}

SIM_UPDATE_DOSSIER = {
    "name": "sim_update_dossier",
    "description": "Record a fact learned about the user during conversation.",
    "parameters": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Category of the fact (e.g. 'Name', 'Occupation', 'Interest').",
            },
            "value": {
                "type": "string",
                "description": "The fact learned.",
            },
        },
        "required": ["key", "value"],
    },
}

SIM_ASSESS_BOREDOM = {
    "name": "sim_assess_boredom",
    "description": (
        "Set the persona's engagement level. High boredom = persona wants to leave. "
        "Increase boredom for repetitive/short inputs, decrease for interesting ones."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "boredom_level": {
                "type": "integer",
                "description": "Boredom 0-10. 0=fascinated, 10=leaving.",
            },
            "reason": {
                "type": "string",
                "description": "Why this engagement level.",
            },
        },
        "required": ["boredom_level", "reason"],
    },
}

SIM_TRIGGER_DISTRACTION = {
    "name": "sim_trigger_distraction",
    "description": (
        "Introduce an environmental distraction (phone ring, friend arriving, etc). "
        "Use sparingly to test the user's ability to re-engage."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "distraction_level": {
                "type": "integer",
                "description": "0=focused, 10=completely distracted.",
            },
            "source": {
                "type": "string",
                "description": "Source of distraction (e.g. 'Phone ringing').",
            },
        },
        "required": ["distraction_level", "source"],
    },
}

SIM_GRADE_RESPONSE = {
    "name": "sim_grade_response",
    "description": (
        "Grade the user's social skill using the S.C.A.L.E. and R.E.A.D. frameworks. "
        "Call after processing the user's message to provide feedback."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_score": {
                "type": "integer",
                "description": "Overall skill score 0-100 (relevance, engagement, technique, wit).",
            },
            "warmth_score": {
                "type": "integer",
                "description": "Warmth score 0-100 (validation, empathy, inclusive language).",
            },
            "critique": {
                "type": "string",
                "description": "Specific feedback — cite the user's words.",
            },
            "better_version": {
                "type": "string",
                "description": "Same intent, better technique.",
            },
            "wit_mechanic": {
                "type": "string",
                "description": "R.E.A.D. mechanic used, if any.",
                "enum": ["Reframe", "Exaggerate", "Associate", "Defy", "None"],
            },
        },
        "required": ["skill_score", "warmth_score", "critique"],
    },
}

SIM_GET_SCENARIO = {
    "name": "sim_get_scenario",
    "description": (
        "Fetch a training scenario from the golden set. "
        "Call this to start a new drill round."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "difficulty": {
                "type": "integer",
                "description": "Scenario difficulty level (1=basic, 2=intermediate, 3=advanced, 4=mastery).",
                "enum": [1, 2, 3, 4],
            },
        },
        "required": ["difficulty"],
    },
}

SIM_DRILL_GRADE = {
    "name": "sim_drill_grade",
    "description": (
        "Grade a user's drill response. Reads scenario context from state, "
        "runs offline analysis, updates mood/trust, returns scorecard. "
        "Called by the RapidPro flow webhook — not by Hermes directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_input": {
                "type": "string",
                "description": "The user's response text to grade.",
            },
        },
        "required": ["user_input"],
    },
}

SIM_FREETEXT = {
    "name": "sim_freetext",
    "description": (
        "Handle free-text input that doesn't match a menu option. "
        "Routes to RiveBot first; if no match, forwards to Hermes AI."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_input": {
                "type": "string",
                "description": "The user's free-text message.",
            },
        },
        "required": ["user_input"],
    },
}

SIM_SET_LANGUAGE = {
    "name": "sim_set_language",
    "description": "Set the user's preferred language for Social-Code training.",
    "parameters": {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "description": "Language name or ISO code (English, Kreyòl, Español, Français, en, ht, es, fr).",
            },
        },
        "required": ["language"],
    },
}

SIM_SESSION_SUMMARY = {
    "name": "sim_session_summary",
    "description": "Show aggregated stats for the current training session.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

SIM_TOGGLE_AI = {
    "name": "sim_toggle_ai",
    "description": "Toggle AI-powered grading on/off. Off = qualitative feedback. On = numeric scores + FSRS.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}
