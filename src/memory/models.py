"""Memory configuration and domain models (pure Pydantic, no DB dependency)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MemoryConfig(BaseModel):
    """Tunable knobs for the memory system."""

    enabled: bool = True
    max_memories: int = 20
    max_tokens: int = 2000
    summarize_after: int = 50  # turns before summarisation kicks in
    importance_threshold: float = Field(default=0.7, ge=0.0, le=1.0)


class MemoryEntry(BaseModel):
    """In-memory representation of a single user memory."""

    id: int | None = None
    user_id: str
    key: str
    value: str
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None


class ExtractedFact(BaseModel):
    """A fact extracted from a conversation turn by the MemoryExtractor."""

    key: str = Field(description="Short snake_case identifier, e.g. 'nombre', 'ciudad'")
    value: str = Field(description="The value to remember")
    importance: float = Field(default=0.7, ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    """Batch of facts extracted from a single interaction."""

    facts: list[ExtractedFact] = Field(default_factory=list)
