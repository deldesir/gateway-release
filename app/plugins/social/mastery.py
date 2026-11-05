"""Social-Code — Mastery Tracking & FSRS Scheduling.

PostgreSQL-backed spaced repetition for the WhatsApp gateway.
Ported from social-code/apps/small_talk's MasteryService (SQLite)
to async PostgreSQL using the shared temba database.

Tables (auto-created in init_db):
  social_drill_history   — every drill attempt with scores
  social_card_schedule   — FSRS state per scenario per user
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fsrs import Card, Rating, Scheduler, State

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
#  FSRS Score Mapping (matches social_core.grading)
# ════════════════════════════════════════════════════════════════════════════

FSRS_LABELS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


def score_to_fsrs_rating(score: int) -> Rating:
    """Map a 0-100 composite score to an FSRS Rating enum.

    Thresholds match social_core.grading.score_to_fsrs_suggestion().
    """
    if score < 40:
        return Rating.Again
    elif score < 65:
        return Rating.Hard
    elif score < 85:
        return Rating.Good
    else:
        return Rating.Easy


def composite_score(skill: int, warmth: int) -> int:
    """Warmth-calibrated composite score (60% warmth, 40% skill)."""
    return int(skill * 0.4 + warmth * 0.6)


# ════════════════════════════════════════════════════════════════════════════
#  Database Operations (sync — called from webhook handlers)
# ════════════════════════════════════════════════════════════════════════════

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS social_drill_history (
    id              SERIAL PRIMARY KEY,
    user_urn        TEXT NOT NULL,
    scenario_key    TEXT NOT NULL,
    app_slug        TEXT DEFAULT '',
    difficulty      INTEGER DEFAULT 1,
    skill_score     INTEGER DEFAULT 0,
    warmth_score    INTEGER DEFAULT 0,
    composite       INTEGER DEFAULT 0,
    fsrs_rating     INTEGER DEFAULT 3,
    lang            TEXT DEFAULT 'en',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS social_card_schedule (
    id              SERIAL PRIMARY KEY,
    user_urn        TEXT NOT NULL,
    scenario_key    TEXT NOT NULL,
    due             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stability       REAL DEFAULT 0,
    difficulty_fsrs REAL DEFAULT 0,
    elapsed_days    INTEGER DEFAULT 0,
    scheduled_days  INTEGER DEFAULT 0,
    reps            INTEGER DEFAULT 0,
    state           INTEGER DEFAULT 0,
    last_review     TIMESTAMPTZ,
    UNIQUE(user_urn, scenario_key)
);

CREATE INDEX IF NOT EXISTS idx_card_schedule_due
    ON social_card_schedule(user_urn, due);
CREATE INDEX IF NOT EXISTS idx_drill_history_user
    ON social_drill_history(user_urn, created_at DESC);
"""


def _get_sync_conn():
    """Get a synchronous psycopg2 connection to the temba database."""
    import os
    import psycopg2

    # The same database the gateway uses (POSTGRES_URI in .env), unless DATABASE_URL names
    # another. Never a literal credential: the cluster checks passwords, and a default that
    # happened to work once was the only reason this ever connected.
    uri = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URI") or ""
    if not uri:
        raise RuntimeError("no database configured for mastery (set POSTGRES_URI or DATABASE_URL)")

    # Convert asyncpg URI to psycopg2 format
    uri = uri.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(uri)


def ensure_tables():
    """Create mastery tables if they don't exist."""
    try:
        conn = _get_sync_conn()
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLES)
        conn.commit()
        conn.close()
        logger.info("[mastery] Tables ensured")
    except Exception as e:
        logger.warning("[mastery] Could not create tables: %s", e)


def record_drill(
    user_urn: str,
    scenario_key: str,
    app_slug: str,
    difficulty: int,
    skill: int,
    warmth: int,
    lang: str = "en",
) -> Dict:
    """Record a drill attempt and update FSRS schedule.

    Returns a dict with FSRS feedback for the scorecard.
    """
    comp = composite_score(skill, warmth)
    rating = score_to_fsrs_rating(comp)
    rating_int = {Rating.Again: 1, Rating.Hard: 2, Rating.Good: 3, Rating.Easy: 4}[rating]
    label = FSRS_LABELS[rating_int]

    try:
        conn = _get_sync_conn()
        with conn.cursor() as cur:
            # 1. Record the attempt
            cur.execute(
                """INSERT INTO social_drill_history
                   (user_urn, scenario_key, app_slug, difficulty,
                    skill_score, warmth_score, composite, fsrs_rating, lang)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (user_urn, scenario_key, app_slug, difficulty,
                 skill, warmth, comp, rating_int, lang),
            )

            # 2. Update FSRS schedule
            now = datetime.now(timezone.utc)
            scheduler = Scheduler()

            # Load existing card state
            cur.execute(
                """SELECT stability, difficulty_fsrs, state, last_review, reps
                   FROM social_card_schedule
                   WHERE user_urn = %s AND scenario_key = %s""",
                (user_urn, scenario_key),
            )
            row = cur.fetchone()

            card = Card()
            if row:
                stab, diff, st, lr, reps = row
                card.stability = stab
                card.difficulty = diff
                card.state = State(st)
                if lr:
                    card.last_review = lr
            else:
                reps = 0

            new_card, _ = scheduler.review_card(card, rating, now)
            scheduled_days = max(0, (new_card.due - now).days)
            elapsed_days = max(0, (now - card.last_review).days) if card.last_review else 0

            cur.execute(
                """INSERT INTO social_card_schedule
                   (user_urn, scenario_key, due, stability, difficulty_fsrs,
                    elapsed_days, scheduled_days, reps, state, last_review)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_urn, scenario_key) DO UPDATE SET
                    due = EXCLUDED.due,
                    stability = EXCLUDED.stability,
                    difficulty_fsrs = EXCLUDED.difficulty_fsrs,
                    elapsed_days = EXCLUDED.elapsed_days,
                    scheduled_days = EXCLUDED.scheduled_days,
                    reps = social_card_schedule.reps + 1,
                    state = EXCLUDED.state,
                    last_review = EXCLUDED.last_review""",
                (user_urn, scenario_key, new_card.due, new_card.stability,
                 new_card.difficulty, elapsed_days, scheduled_days,
                 reps + 1, new_card.state.value, now),
            )

        conn.commit()
        conn.close()

        # Build review interval description
        if scheduled_days == 0:
            interval_text = "soon"
        elif scheduled_days == 1:
            interval_text = "tomorrow"
        elif scheduled_days < 7:
            interval_text = f"in {scheduled_days} days"
        elif scheduled_days < 30:
            weeks = scheduled_days // 7
            interval_text = f"in {weeks} week{'s' if weeks > 1 else ''}"
        else:
            months = scheduled_days // 30
            interval_text = f"in {months} month{'s' if months > 1 else ''}"

        return {
            "rating": rating_int,
            "label": label,
            "composite": comp,
            "scheduled_days": scheduled_days,
            "interval_text": interval_text,
            "reps": reps + 1,
        }
    except Exception as e:
        logger.warning("[mastery] record_drill failed: %s", e)
        return {
            "rating": rating_int,
            "label": label,
            "composite": comp,
            "scheduled_days": 0,
            "interval_text": "N/A",
            "reps": 0,
        }


def get_due_scenario_keys(user_urn: str, limit: int = 5) -> List[str]:
    """Return scenario keys that are due for FSRS review, ordered by most overdue."""
    try:
        conn = _get_sync_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT scenario_key FROM social_card_schedule
                   WHERE user_urn = %s AND due <= NOW()
                   ORDER BY due ASC LIMIT %s""",
                (user_urn, limit),
            )
            keys = [row[0] for row in cur.fetchall()]
        conn.close()
        return keys
    except Exception as e:
        logger.warning("[mastery] get_due_scenario_keys failed: %s", e)
        return []


def get_seen_scenario_keys(user_urn: str) -> set:
    """Return set of scenario keys the user has ever drilled."""
    try:
        conn = _get_sync_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT scenario_key FROM social_drill_history WHERE user_urn = %s",
                (user_urn,),
            )
            keys = {row[0] for row in cur.fetchall()}
        conn.close()
        return keys
    except Exception as e:
        logger.warning("[mastery] get_seen_scenario_keys failed: %s", e)
        return set()


def get_session_stats(user_urn: str, since_minutes: int = 60) -> Dict:
    """Get aggregated stats for the current session (last N minutes)."""
    try:
        conn = _get_sync_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) as rounds,
                          COALESCE(AVG(composite), 0) as avg_score,
                          COALESCE(AVG(skill_score), 0) as avg_skill,
                          COALESCE(AVG(warmth_score), 0) as avg_warmth
                   FROM social_drill_history
                   WHERE user_urn = %s
                     AND created_at >= NOW() - INTERVAL '%s minutes'""",
                (user_urn, since_minutes),
            )
            row = cur.fetchone()
        conn.close()
        if row:
            return {
                "rounds": row[0],
                "avg_score": round(row[1]),
                "avg_skill": round(row[2]),
                "avg_warmth": round(row[3]),
            }
    except Exception as e:
        logger.warning("[mastery] get_session_stats failed: %s", e)
    return {"rounds": 0, "avg_score": 0, "avg_skill": 0, "avg_warmth": 0}
