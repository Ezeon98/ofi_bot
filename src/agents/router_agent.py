"""RouterAgent — the single PydanticAI agent for ServiMatch.

Responsibilities:
  - Classify intent
  - Extract entities
  - Decide which tools to call
  - Return a structured AgentResponse

Business logic lives in tools and services, NOT here.

The agent is constructed once (module-level singleton) and reused across
requests. Dependencies are injected per-call via RunContext[AgentDependencies].
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic_ai import Agent, RunContext

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse
from src.agents.prompts.router import ROUTER_SYSTEM_PROMPT
from src.tools.business.providers import (
    BuscarPrestadoresInput,
    RubrosRelacionadosInput,
    ResolverUbicacionInput,
    CrearPrestadorInput,
    ActualizarPrestadorInput,
    ConsultarPrestadorInput,
    buscar_prestadores,
    buscar_rubros_relacionados,
    resolver_ubicacion,
    build_provider_search_report,
    crear_prestador,
    actualizar_prestador,
    consultar_prestador,
)
from src.tools.business.search_state import (
    ConsultarEstadoBusquedaInput,
    GuardarEstadoBusquedaInput,
    LimpiarEstadoBusquedaInput,
    consultar_estado_busqueda,
    guardar_estado_busqueda,
    limpiar_estado_busqueda,
)
from src.tools.memory.memory_tools import (
    ActualizarMemoriaInput,
    BuscarMemoriaInput,
    GuardarMemoriaInput,
    actualizar_memoria,
    buscar_memoria,
    guardar_memoria,
)

logger = logging.getLogger(__name__)

# ── Anti-loop guard ───────────────────────────────────────────────────────────
# Keeps the last tool call signature so we can detect an identical repeated call
# (the LLM re-invokes the same tool with the same params in a loop).
_last_tool_call: dict[str, object] = {}
_last_tool_time: float = 0.0
_ANTI_LOOP_WINDOW_S = 30  # seconds within which we consider a call a duplicate


def _build_location_label_from_params(params: object) -> str:
    """Build a location label from barrio and ciudad attributes on the params."""
    barrio = getattr(params, "barrio", None) or ""
    ciudad = getattr(params, "ciudad", None) or ""
    parts = [p for p in [barrio, ciudad] if p]
    return ", ".join(parts) if parts else "la ubicación actual"


def _is_repeat_call(tool_name: str, params: object) -> Any | None:
    """Return a descriptive dict if the exact same call was made recently."""
    global _last_tool_call, _last_tool_time
    now = time.monotonic()
    sig: dict[str, object] = {"tool": tool_name, "params": str(params)}
    if (
        sig == _last_tool_call
        and now - _last_tool_time < _ANTI_LOOP_WINDOW_S
    ):
        logger.warning(
            "ANTI_LOOP detected repeat call tool=%s params=%s",
            tool_name, params,
        )
        location_label = _build_location_label_from_params(params)
        return {
            "info": "duplicate_call_blocked",
            "message": (
                f"Ya buscaste {params.rubro} en {location_label} "
                "y no se encontraron resultados. No tiene sentido repetir la misma "
                "búsqueda. Informale al usuario que no hay resultados y sugiere "
                "alternativas (otro rubro, otra ubicación, o esperar a que se registren nuevos prestadores)."
            ),
            "rubro": params.rubro,
        }
    _last_tool_call = sig
    _last_tool_time = now
    return None


# ── Agent definition ─────────────────────────────────────────────────────────
# model is set at runtime via AIOrchestrator to allow runtime config changes.
# We default to gpt-4o-mini; the orchestrator overrides via the `model` param.

router_agent: Agent[AgentDependencies, AgentResponse] = Agent(
    model="openai-chat:gpt-4o-mini",
    deps_type=AgentDependencies,
    output_type=AgentResponse,
    system_prompt=ROUTER_SYSTEM_PROMPT,
)


# ── Register tools ────────────────────────────────────────────────────────────
# Tools are registered directly on the agent; no magic registry needed here.
# The ToolRegistry is available for future dynamic loading scenarios.


@router_agent.tool
async def tool_buscar_prestadores(
    ctx: RunContext[AgentDependencies],
    params: BuscarPrestadoresInput,
) -> dict:
    """Find service providers matching a rubro and optional location.

    Returns a structured report with `provider_count`, `providers`, `related_rubros`
    and `sufficient_results` so the LLM can decide whether to ask for missing
    info, broaden the rubro, or respond with the current shortlist.
    """
    blocked = _is_repeat_call("tool_buscar_prestadores", params)
    if blocked is not None:
        return build_provider_search_report(
            params,
            [],
            status="duplicate_call_blocked",
            message=blocked["message"],
        )

    providers = await buscar_prestadores(ctx, params)
    if not providers:
        logger.info(
            "PROVIDER_ZERO_RESULTS tool_name=tool_buscar_prestadores "
            "rubro=%r barrio=%r ciudad=%r lat=%r lon=%r",
            params.rubro, params.barrio, params.ciudad, params.lat, params.lon,
        )
    return build_provider_search_report(params, providers)


@router_agent.tool
async def tool_rubros_relacionados(
    ctx: RunContext[AgentDependencies],
    params: RubrosRelacionadosInput,
) -> dict:
    """Return close canonical trade alternatives for a search broadening step."""
    return await buscar_rubros_relacionados(ctx, params)


@router_agent.tool
async def tool_resolver_ubicacion(
    ctx: RunContext[AgentDependencies],
    params: ResolverUbicacionInput,
) -> dict:
    """Normalize a user-mentioned zone into barrio/ciudad and optional coords."""
    return await resolver_ubicacion(ctx, params)


@router_agent.tool
async def tool_crear_prestador(
    ctx: RunContext[AgentDependencies],
    params: CrearPrestadorInput,
) -> dict:
    """Register the current user as a service provider."""
    return await crear_prestador(ctx, params)


@router_agent.tool
async def tool_actualizar_prestador(
    ctx: RunContext[AgentDependencies],
    params: ActualizarPrestadorInput,
) -> dict:
    """Update a field in the current user's provider profile."""
    return await actualizar_prestador(ctx, params)


@router_agent.tool
async def tool_consultar_prestador(
    ctx: RunContext[AgentDependencies],
    params: ConsultarPrestadorInput,
) -> dict:
    """Return the current user's provider profile."""
    return await consultar_prestador(ctx, params)


@router_agent.tool
async def tool_consultar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    params: ConsultarEstadoBusquedaInput,
) -> dict:
    """Read the active guided-search state for the current user."""
    return await consultar_estado_busqueda(ctx, params)


@router_agent.tool
async def tool_guardar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    params: GuardarEstadoBusquedaInput,
) -> dict:
    """Persist the guided-search state for the current user."""
    return await guardar_estado_busqueda(ctx, params)


@router_agent.tool
async def tool_limpiar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    params: LimpiarEstadoBusquedaInput,
) -> dict:
    """Clear the guided-search state for the current user."""
    return await limpiar_estado_busqueda(ctx, params)


@router_agent.tool
async def tool_guardar_memoria(
    ctx: RunContext[AgentDependencies],
    params: GuardarMemoriaInput,
) -> dict:
    """Persist a fact about the user for future conversations."""
    return await guardar_memoria(ctx, params)


@router_agent.tool
async def tool_buscar_memoria(
    ctx: RunContext[AgentDependencies],
    params: BuscarMemoriaInput,
) -> dict:
    """Look up a specific stored fact about the user."""
    return await buscar_memoria(ctx, params)


@router_agent.tool
async def tool_actualizar_memoria(
    ctx: RunContext[AgentDependencies],
    params: ActualizarMemoriaInput,
) -> dict:
    """Update a stored fact about the user."""
    return await actualizar_memoria(ctx, params)