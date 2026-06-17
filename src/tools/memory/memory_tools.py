"""Memory tools exposed to the LLM.

These let the agent explicitly read/write persistent user facts when it
detects information worth remembering (name, city, profession, etc.).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from src.agents.dependencies import AgentDependencies


class GuardarMemoriaInput(BaseModel):
    key: str = Field(description="Short snake_case key, e.g. 'nombre', 'ciudad', 'rubro_preferido'")
    value: str = Field(description="Value to store")
    importance: float = Field(default=0.8, ge=0.0, le=1.0)


class BuscarMemoriaInput(BaseModel):
    key: str = Field(description="Exact key to look up")


class ActualizarMemoriaInput(BaseModel):
    key: str
    value: str
    importance: float = Field(default=0.8, ge=0.0, le=1.0)


async def guardar_memoria(
    ctx: RunContext[AgentDependencies],
    params: GuardarMemoriaInput,
) -> dict[str, str]:
    """Persist a fact about the user so it can be recalled in future conversations."""
    await ctx.deps.memory_service.upsert_memory(
        ctx.deps.user_id, params.key, params.value, params.importance
    )
    return {"status": "guardado", "key": params.key}


async def buscar_memoria(
    ctx: RunContext[AgentDependencies],
    params: BuscarMemoriaInput,
) -> dict[str, str | None]:
    """Look up a specific memory key for the current user."""
    memories = await ctx.deps.memory_service.get_memories(ctx.deps.user_id)
    match = next((m for m in memories if m.key == params.key), None)
    return {"key": params.key, "value": match.value if match else None}


async def actualizar_memoria(
    ctx: RunContext[AgentDependencies],
    params: ActualizarMemoriaInput,
) -> dict[str, str]:
    """Update an existing memory value (upserts if missing)."""
    await ctx.deps.memory_service.upsert_memory(
        ctx.deps.user_id, params.key, params.value, params.importance
    )
    return {"status": "actualizado", "key": params.key}
