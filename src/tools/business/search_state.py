"""Conversation-state tools for guided service searches."""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from src.agents.dependencies import AgentDependencies
from src.infrastructure.database.repositories.estado import EstadoRepository

SEARCH_STATE_NAME = "guided_provider_search"


class ConsultarEstadoBusquedaInput(BaseModel):
    """Read the current guided-search state for the active user."""


class GuardarEstadoBusquedaInput(BaseModel):
    """Persist the current guided-search step and captured entities."""

    paso: str = Field(
        description="Current search step, e.g. 'awaiting_need', 'awaiting_zone', 'ready_to_search'"
    )
    rubro: str | None = Field(default=None, description="Captured trade or need")
    barrio: str | None = Field(default=None, description="Captured neighborhood or district")
    ciudad: str | None = Field(default=None, description="Captured city or locality")
    detalle: str | None = Field(
        default=None,
        description="Optional free-text detail for the search, e.g. 'urgente'",
    )


class LimpiarEstadoBusquedaInput(BaseModel):
    """Clear the active guided-search state for the current user."""


async def consultar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    _params: ConsultarEstadoBusquedaInput,
) -> dict[str, str | bool | None]:
    """Return the current guided-search state for the user, if any."""
    state = await EstadoRepository(ctx.deps.db).get(ctx.deps.user_id)
    if state.get("estado") != SEARCH_STATE_NAME:
        return {"activo": False, "paso": None, "rubro": None, "barrio": None, "ciudad": None, "detalle": None}

    return {
        "activo": True,
        "paso": state.get("paso"),
        "rubro": state.get("rubro"),
        "barrio": state.get("barrio"),
        "ciudad": state.get("ciudad"),
        "detalle": state.get("detalle"),
    }


async def guardar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    params: GuardarEstadoBusquedaInput,
) -> dict[str, str | bool | None]:
    """Upsert guided-search state so the next user turn can resume cleanly."""
    payload = {
        "estado": SEARCH_STATE_NAME,
        "paso": params.paso,
        "rubro": params.rubro,
        "barrio": params.barrio,
        "ciudad": params.ciudad,
        "detalle": params.detalle,
    }
    await EstadoRepository(ctx.deps.db).save(ctx.deps.user_id, payload)
    return {"guardado": True, **payload}


async def limpiar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    _params: LimpiarEstadoBusquedaInput,
) -> dict[str, bool]:
    """Delete the guided-search state for the current user."""
    await EstadoRepository(ctx.deps.db).delete(ctx.deps.user_id)
    return {"limpiado": True}