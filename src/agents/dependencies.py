"""PydanticAI Dependency Injection container.

Passed to every tool via RunContext[AgentDependencies].
No globals, no singletons reaching into tools directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.models import MemoryConfig
from src.memory.service import MemoryService


@dataclass
class AgentDependencies:
    """Everything a tool needs, injected at call time."""

    db: AsyncSession
    user_id: str  # telefono — used for state repo, logging and external comms
    usuario_id: int  # usuarios.id — used for memory/conversation DB operations
    memory_service: MemoryService
    memory_config: MemoryConfig
    current_message_metadata: dict[str, Any] | None = None
