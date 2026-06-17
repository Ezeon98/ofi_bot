"""Pydantic schemas used as API contracts for the memory system."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MemoryRead(BaseModel):
    id: int
    user_id: str
    key: str
    value: str
    importance: float
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class MemoryUpsert(BaseModel):
    key: str
    value: str
    importance: float = Field(default=0.7, ge=0.0, le=1.0)
    expires_at: datetime | None = None


class ConversationTurnRead(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    intent: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationRead(BaseModel):
    id: int
    user_id: str
    started_at: datetime
    last_message_at: datetime
    summary: str | None = None

    model_config = {"from_attributes": True}
