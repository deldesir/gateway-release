"""Social-Code — Offline Grading & Simulation Engine.

Rule-based persona response generation and skill grading for zero-API training.
Ported from social-code/apps/small_talk/src/small_talk/core/offline.py.

Used as the fallback when Hermes/Gemini is unavailable, providing the
golden-set experience without any LLM calls.
"""

import random
import re
from typing import Any, Dict, List, Tuple


# ════════════════════════════════════════════════════════════════════════════
#  Mood State Machine
# ════════════════════════════════════════════════════════════════════════════

MOOD_TRANSITIONS = {
    ("Neutral", "positive"): "Friendly",
    ("Neutral", "negative"): "Annoyed",
    ("Neutral", "question"): "Curious",
    ("Neutral", "neutral"): "Neutral",
    ("Friendly", "positive"): "Happy",
    ("Friendly", "negative"): "Confused",
    ("Friendly", "question"): "Curious",
    ("Happy", "positive"): "Happy",
    ("Happy", "negative"): "Confused",
    ("Curious", "positive"): "Impressed",
    ("Curious", "negative"): "Suspicious",
    ("Curious", "question"): "Curious",
    ("Annoyed", "positive"): "Neutral",
    ("Annoyed", "negative"): "Hostile",
    ("Annoyed", "question"): "Suspicious",
    ("Hostile", "positive"): "Annoyed",
    ("Hostile", "negative"): "Hostile",
    ("Confused", "positive"): "Neutral",
    ("Confused", "negative"): "Annoyed",
    ("Suspicious", "positive"): "Neutral",
    ("Suspicious", "negative"): "Hostile",
    ("Impressed", "positive"): "Happy",
    ("Impressed", "question"): "Curious",
}


# ════════════════════════════════════════════════════════════════════════════
#  Sentiment Detection (multilingual)
# ════════════════════════════════════════════════════════════════════════════

SENTIMENT_SIGNALS = {
    "en": {
        "positive": {"nice", "great", "love", "awesome", "cool", "amazing", "beautiful",
                     "thank", "thanks", "appreciate", "kind", "wonderful", "excellent",
                     "good", "happy", "glad", "enjoy", "please", "sorry", "excuse",
                     "interesting", "wow", "fun", "lovely", "sweet", "perfect"},
        "negative": {"hate", "stupid", "ugly", "annoying", "boring", "terrible", "awful",
                     "shut up", "go away", "leave", "no", "never", "wrong", "bad",
                     "idiot", "dumb", "worst", "useless", "pathetic", "ridiculous"},
        "questions": {"?", "what", "where", "when", "who", "why", "how", "do you",
                      "are you", "have you"},
    },
    "es": {
        "positive": {"bien", "genial", "amor", "increíble", "guay", "asombroso",
                     "hermoso", "gracias", "aprecio", "amable", "maravilloso",
                     "excelente", "bueno", "feliz", "alegre", "perfecto"},
        "negative": {"odio", "estúpido", "feo", "molesto", "aburrido", "terrible",
                     "horrible", "cállate", "vete", "fuera", "no", "nunca", "mal",
                     "idiota", "tonto", "peor", "inútil", "ridículo"},
        "questions": {"?", "qué", "donde", "cuándo", "quién", "por qué", "cómo"},
    },
    "fr": {
        "positive": {"bien", "super", "aime", "génial", "cool", "incroyable", "beau",
                     "merci", "apprécie", "gentil", "merveilleux", "excellent", "bon",
                     "heureux", "content", "parfait"},
        "negative": {"déteste", "stupide", "laid", "agaçant", "ennuyeux", "terrible",
                     "affreux", "tais-toi", "va-t-en", "pars", "non", "jamais",
                     "mauvais", "idiot", "pire", "inutile", "ridicule"},
        "questions": {"?", "quoi", "où", "quand", "qui", "pourquoi", "comment"},
    },
    "ht": {
        "positive": {"bon", "bèl", "renmen", "chanse", "fre", "ekselan", "mèsi",
                     "apresye", "janti", "kontan", "plezi", "souple", "padon",
                     "enteresan", "wow", "amizan", "dous", "pafè"},
        "negative": {"rayi", "sòt", "lèd", "anmèdan", "anniyan", "terib", "move",
                     "fèmen bouch", "ale", "pa", "jamais", "pòv", "vye", "enbesil",
                     "pi mal", "initil", "ridikil"},
        "questions": {"?", "kisa", "ki kote", "ki lè", "kilès", "poukisa", "kijan",
                      "èske ou"},
    },
}


def detect_sentiment(text: str, lang: str = "en") -> str:
    """Classify input as positive/negative/question/neutral."""
    lower = text.lower()
    words = set(lower.split())
    signals = SENTIMENT_SIGNALS.get(lang, SENTIMENT_SIGNALS["en"])

    if "?" in text:
        return "question"

    # Count hits: single words via set intersection, multi-word via substring
    def _count_hits(signal_set):
        count = 0
        for s in signal_set:
            if " " in s:
                if s in lower:
                    count += 1
            elif s in words:
                count += 1
        return count

    pos_hits = _count_hits(signals["positive"])
    neg_hits = _count_hits(signals["negative"])

    if neg_hits > pos_hits:
        return "negative"
    if pos_hits > 0:
        return "positive"
    return "neutral"


def compute_trust_delta(text: str, current_trust: int, lang: str = "en") -> int:
    """Rule-based trust change."""
    lower = text.lower()
    sentiment = detect_sentiment(text, lang)
    delta = 0

    triggers = {
        "en": {
            "you": ["you", "your"],
            "disclosure": ["i feel", "i think", "honestly", "for me"],
            "humor": ["haha", "lol", "funny", "joke"],
        },
        "ht": {
            "you": ["ou", "pou ou"],
            "disclosure": ["m santi", "m panse", "onètman", "pou mwen"],
            "humor": ["haha", "lol", "amizan", "blag"],
        },
    }
    lang_triggers = triggers.get(lang, triggers["en"])

    if "?" in text and any(w in lower for w in lang_triggers["you"]):
        delta += random.randint(2, 4)
    elif "?" in text:
        delta += random.randint(1, 2)
    if any(phrase in lower for phrase in lang_triggers["disclosure"]):
        delta += random.randint(1, 3)
    if any(w in lower for w in lang_triggers["humor"]):
        delta += random.randint(1, 2)
    if sentiment == "negative":
        delta -= random.randint(3, 8)
    if sentiment == "positive" and delta == 0:
        delta += random.randint(1, 2)
    if delta == 0:
        delta = random.choice([-1, 0, 0, 1])

    return delta


# ════════════════════════════════════════════════════════════════════════════
#  Offline Response Templates
# ════════════════════════════════════════════════════════════════════════════

RESPONSE_TEMPLATES = {
    "en": {
        ("Neutral", "low"): ["Mhm.", "Yeah.", "Okay.", "Sure.", "Right.", "*nods*"],
        ("Annoyed", "low"): ["Look, I'm busy.", "Not really in the mood.",
                             "Can I help you with something?"],
        ("Hostile", "low"): ["Please leave me alone.", "I don't think so.",
                            "We're done here."],
        ("Friendly", "mid"): ["Ha, yeah that's true!", "Oh really? That's cool.",
                              "I can see that."],
        ("Happy", "high"): ["This is nice. I'm glad we ran into each other.",
                           "You're fun to talk to!", "I like your energy."],
    },
    "ht": {
        ("Neutral", "low"): ["Mhm.", "Wi.", "Oke.", "Sèten.", "Se sa.", "*souke tèt*"],
        ("Annoyed", "low"): ["Ekoute, m okipe.", "M pa tèlman anvi pale.",
                             "Èske m ka ede w ak kichòy?"],
        ("Hostile", "low"): ["Tanpri kite m anrepo.", "M pa kwè sa.",
                            "Nou fini la a."],
        ("Friendly", "mid"): ["Ha, se vre wi!", "Tout bon? Sa bèl wi.",
                              "M wè sa."],
        ("Happy", "high"): ["Sa bon wi. M kontan nou kwaze.",
                           "Ou amizan anpil!", "M renmen jan w pale a."],
    },
}


def _get_trust_bucket(trust: int) -> str:
    if trust < 30:
        return "low"
    elif trust <= 60:
        return "mid"
    return "high"


def generate_offline_response(
    user_input: str,
    mood: str,
    trust_score: int,
    boredom_level: int,
    persona: str = "A stranger",
    lang: str = "en",
) -> Tuple[str, Dict[str, Any]]:
    """Generate a rule-based persona response (no AI)."""
    sentiment = detect_sentiment(user_input, lang)
    trust_delta = compute_trust_delta(user_input, trust_score, lang)
    new_trust = max(0, min(100, trust_score + trust_delta))
    new_mood = MOOD_TRANSITIONS.get((mood, sentiment), mood)

    word_count = len(user_input.split())
    new_boredom = max(0, min(10, boredom_level + (1 if word_count < 4 else -1)))

    bucket = _get_trust_bucket(new_trust)
    lang_templates = RESPONSE_TEMPLATES.get(lang, RESPONSE_TEMPLATES["en"])
    key = (new_mood, bucket)
    templates = lang_templates.get(key)
    if not templates:
        templates = lang_templates.get(("Neutral", bucket), ["..."])
    response = random.choice(templates)

    state_updates = {
        "mood": new_mood,
        "trust_score": new_trust,
        "boredom_level": new_boredom,
        "distraction_level": 0,
        "internal_monologue": f"[Offline] {sentiment} detected.",
    }
    return response, state_updates


# ════════════════════════════════════════════════════════════════════════════
#  Offline Training Analysis (skill/warmth scoring)
# ════════════════════════════════════════════════════════════════════════════

def analyze_response_offline(
    context: str, user_response: str, lang: str = "en"
) -> Dict[str, Any]:
    """Offline analysis of a user response — keyword-based skill/warmth scoring.

    Returns scores PLUS actionable feedback, ideal response, and tips.
    """
    lower = user_response.lower()
    words = lower.split()
    word_count = len(words)

    # ── Skill scoring ──
    skill = 30
    skill_hits = []
    skill_misses = []

    if word_count >= 8:
        skill += 10
        skill_hits.append("good length")
    else:
        skill_misses.append("too short — elaborate more")

    if "?" in user_response:
        skill += 15
        skill_hits.append("asked a question")
    else:
        skill_misses.append("no question asked — try engaging them")

    # ── Warmth scoring ──
    warmth = 30
    warmth_hits = []
    warmth_misses = []

    keywords = {
        "en": {
            "validation": ["makes sense", "understand", "i see", "hear you", "that's true",
                           "you're right", "absolutely", "exactly"],
            "inclusive": ["we", "us", "together", "let's"],
            "you": ["you", "your"],
            "i": ["i ", "i'm", "i've"],
            "empathy": ["feel", "must be", "sounds like", "i imagine", "that's tough",
                        "i appreciate"],
            "humor": ["haha", "lol", "funny", "😂", "😄"],
            "greeting": ["hey", "hi", "hello", "nice to", "good to"],
        },
        "ht": {
            "validation": ["sa rive", "m konprann", "m wè sa", "se vre"],
            "inclusive": ["nou", "ansanm"],
            "you": ["ou", "pou ou"],
            "i": ["m ", "mwen ", "m ap"],
            "empathy": ["santi", "dwe difisil", "m imajine"],
            "humor": ["haha", "lol", "amizan"],
            "greeting": ["sak pase", "bonswa", "bonjou"],
        },
    }
    lang_kws = keywords.get(lang, keywords["en"])

    if any(p in lower for p in lang_kws["validation"]):
        warmth += 20
        warmth_hits.append("validation")
    else:
        warmth_misses.append("validate their feelings")

    if any(w in lower for w in lang_kws["inclusive"]):
        warmth += 10
        warmth_hits.append("inclusive language")

    if any(w in lower for w in lang_kws["empathy"]):
        warmth += 15
        warmth_hits.append("empathy")
    else:
        warmth_misses.append("show empathy")

    if any(w in lower for w in lang_kws["humor"]):
        warmth += 5
        warmth_hits.append("humor")

    if any(w in lower for w in lang_kws["greeting"]):
        warmth += 5
        warmth_hits.append("friendly opener")
    else:
        warmth_misses.append("start with a warm greeting")

    you_c = sum(lower.count(w) for w in lang_kws["you"])
    i_c = sum(lower.count(w) for w in lang_kws["i"])
    if you_c > i_c:
        warmth += 15
        warmth_hits.append("other-focused")
    elif i_c > you_c and i_c > 1:
        warmth_misses.append("less 'I', more 'you' — focus on them")

    skill = max(0, min(100, skill))
    warmth = max(0, min(100, warmth))

    # ── Build critique ──
    critique_parts = []
    if skill_hits:
        critique_parts.append("✅ " + ", ".join(skill_hits))
    if warmth_hits:
        critique_parts.append("✅ " + ", ".join(warmth_hits))

    tips = []
    for miss in skill_misses[:2]:
        tips.append(f"📌 {miss}")
    for miss in warmth_misses[:2]:
        tips.append(f"📌 {miss}")

    critique = "\n".join(critique_parts + tips) if (critique_parts or tips) else "Keep practicing!"

    return {
        "score": skill,
        "warmth_score": warmth,
        "critique": critique,
    }

