from typing import AsyncGenerator
from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.orm import sessionmaker
import os

# Database URL
# Default to SQLite for local dev, but production uses Postgres
DATABASE_URL = os.getenv("POSTGRES_URI", "sqlite+aiosqlite:///./checkpoints.sqlite")

# Engine
# echo defaults OFF: echo=True logs every SQL statement at INFO, which floods
# the journal (a SELECT 1/BEGIN/ROLLBACK per health probe) and wears the flash
# storage on edge devices. Opt back in for debugging with SQL_ECHO=1.
_SQL_ECHO = os.getenv("SQL_ECHO", "").strip().lower() in ("1", "true", "yes", "on")
engine = create_async_engine(DATABASE_URL, echo=_SQL_ECHO, future=True)

# Session
async_session = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


async def _run_migrations():
    """Add new columns to existing tables if they don't exist.

    Each statement runs in its OWN transaction (engine.begin). This is critical
    on Postgres: a failed statement (e.g. the column already exists) aborts the
    ENTIRE transaction, so sharing one transaction would let a single "column
    slug already exists" poison it and skip every later migration — notably
    allowed_urns — crashing the app on a fresh DB where create_all already made
    slug/language but not allowed_urns. Per-statement transactions isolate the
    failure so the remaining migrations still apply. Works on Postgres + SQLite.
    """
    from sqlalchemy import text

    statements = [
        # Persona columns added after the original model shipped
        "ALTER TABLE konex_personas ADD COLUMN slug VARCHAR",
        "ALTER TABLE konex_personas ADD COLUMN language VARCHAR DEFAULT 'ht'",
        # Persona: per-persona access control (Finding 15)
        "ALTER TABLE konex_personas ADD COLUMN allowed_urns JSON DEFAULT '[]'",
        # Backfill slug from name for legacy rows
        "UPDATE konex_personas SET slug = name WHERE slug IS NULL",
    ]

    for sql in statements:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass  # column already exists / statement not applicable


async def init_db():
    # Create tables in one transaction, then run each migration in its OWN
    # transaction (see _run_migrations) so a failed ALTER can't abort the rest.
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    await _run_migrations()
