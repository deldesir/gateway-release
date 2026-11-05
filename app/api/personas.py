from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Persona, PersonaCreate, PersonaRead, PersonaUpdate

router = APIRouter()

@router.post("/personas/", response_model=PersonaRead)
async def create_persona(
    persona: PersonaCreate,
    session: AsyncSession = Depends(get_session)
):
    db_persona = Persona.model_validate(persona)
    session.add(db_persona)
    await session.commit()
    await session.refresh(db_persona)
    return db_persona

@router.get("/personas/", response_model=List[PersonaRead])
async def read_personas(
    offset: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(get_session)
):
    statement = select(Persona).offset(offset).limit(limit)
    result = await session.execute(statement)
    personas = result.scalars().all()
    return personas

@router.get("/personas/{persona_id}", response_model=PersonaRead)
async def read_persona(
    persona_id: str,
    session: AsyncSession = Depends(get_session)
):
    db_persona = await session.get(Persona, persona_id)
    if not db_persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    return db_persona

@router.patch("/personas/{persona_id}", response_model=PersonaRead)
async def update_persona(
    persona_id: str,
    persona_update: PersonaUpdate,
    session: AsyncSession = Depends(get_session)
):
    db_persona = await session.get(Persona, persona_id)
    if not db_persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    
    persona_data = persona_update.model_dump(exclude_unset=True)
    for key, value in persona_data.items():
        setattr(db_persona, key, value)
        
    session.add(db_persona)
    await session.commit()
    await session.refresh(db_persona)
    return db_persona

@router.delete("/personas/{persona_id}")
async def delete_persona(
    persona_id: str,
    session: AsyncSession = Depends(get_session)
):
    db_persona = await session.get(Persona, persona_id)
    if not db_persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    
    await session.delete(db_persona)
    await session.commit()
    return {"ok": True}
