
import asyncio
import sys
import json

# Add app to path
sys.path.append(".")

from app.db import async_session, init_db, engine
from app.models import Persona
from sqlmodel import select

PERSONAS = [
    {
        "id": "quincaillerie",
        "name": "Quincaillerie du Peuple",
        "personality": "Pragmatic, direct, knowledgeable about construction.",
        "style": "Uses construction terms, calls users 'Boss', speaks simple Kreyòl. Very masculine/industrial tone.",
        "allowed_tools": ["mocks", "mempalace", "session_search", "clarify"]
    },
    {
        "id": "sante-plus",
        "name": "Clinique Sante Plus",
        "personality": "Compassionate, calm, professional, reassuring.",
        "style": "Uses medical terms simply, very polite, formal Kreyòl. Always prioritizes patient safety.",
        "allowed_tools": ["rapidpro", "mempalace", "session_search", "clarify"]
    },
    {
        "id": "immobilier",
        "name": "Immobilier Ayiti",
        "personality": "Sophisticated, persuasive, polished.",
        "style": "Uses 'French-influenced' Kreyòl, professional, uses emojis (🏠, 🔑), focuses on value and luxury.",
        "allowed_tools": ["mocks", "mempalace", "session_search", "clarify"]
    }
]

async def seed():
    print("--- Initializing Database ---")
    await init_db()
    
    print("--- Seeding Personas ---")
    async with async_session() as session:
        for p_data in PERSONAS:
            pid = p_data["id"]
            
            # Check if exists
            statement = select(Persona).where(Persona.id == pid)
            result = await session.execute(statement)
            existing = result.scalar_one_or_none()
            
            if existing:
                print(f"Updating {pid}...")
                existing.name = p_data["name"]
                existing.personality = p_data["personality"]
                existing.style = p_data["style"]
                # Store tools as JSON string since we used TEXT column in migration
                # OR if SQLModel handles it, pass list. 
                # Our migration used TEXT. Let's start with list and see if SQLModel auto-serializes to valid JSON-in-TEXT
                # If not, we might need to json.dumps manually if the field type is not properly mapped to JSON variant for SQLite.
                # In models.py we used sa_column=Column(JSON). SQLAlchemy usually handles JSON->Text for SQLite.
                existing.allowed_tools = p_data["allowed_tools"]
                session.add(existing)
            else:
                print(f"Creating {pid}...")
                persona = Persona(
                    id=pid,
                    name=p_data["name"],
                    personality=p_data["personality"],
                    style=p_data["style"],
                    allowed_tools=p_data["allowed_tools"]
                )
                session.add(persona)
        
        await session.commit()
        print("Seeding Complete.")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(seed())
