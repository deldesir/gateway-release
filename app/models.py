from typing import Optional
from sqlmodel import Field, SQLModel
import uuid

from sqlalchemy import Column, JSON
from typing import List

class PersonaBase(SQLModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    slug: str = Field(index=True, unique=True)  # machine-friendly: "konex-support"
    name: str = Field(index=True)                # human-friendly: "Konex Support"
    personality: str
    style: str
    language: str = Field(default="ht")           # default lang for this persona
    system_prompt: Optional[str] = None
    allowed_tools: List[str] = Field(default=[], sa_column=Column(JSON))
    # Access control: empty list = public (any user), non-empty = restricted to listed URNs
    allowed_urns: List[str] = Field(default=[], sa_column=Column("allowed_urns", JSON))

class Persona(PersonaBase, table=True):
    __tablename__ = "konex_personas"

class PersonaCreate(PersonaBase):
    pass

class PersonaRead(PersonaBase):
    pass

class PersonaUpdate(SQLModel):
    slug: Optional[str] = None
    name: Optional[str] = None
    personality: Optional[str] = None
    style: Optional[str] = None
    language: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    allowed_urns: Optional[List[str]] = None

class KnowledgeItemBase(SQLModel):
    title: str = Field(index=True)
    content: str
    source_uri: Optional[str] = None
    
class KnowledgeItem(KnowledgeItemBase, table=True):
    __tablename__ = "konex_knowledge_items"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    created_at: int = Field(default_factory=lambda: 0) # Placeholder, ideally use datetime
    updated_at: int = Field(default_factory=lambda: 0)

class KnowledgeItemCreate(KnowledgeItemBase):
    pass

class KnowledgeItemRead(KnowledgeItemBase):
    id: str
    created_at: int
    updated_at: int

class ChannelConfig(SQLModel, table=True):
    __tablename__ = "konex_channel_configs"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    channel_phone: str = Field(index=True, unique=True)
    persona_id: str = Field(foreign_key="konex_personas.id")
    knowledge_base_id: Optional[str] = None 
    system_prompt_override: Optional[str] = None

