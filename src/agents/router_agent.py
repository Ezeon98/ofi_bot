"""Specialized PydanticAI agents for MiOficio flows.

This module exposes two real agents:
- provider_search_agent: only search-related tools plus mode switching
- provider_profile_agent: only provider-profile tools plus mode switching

A compatibility alias named ``router_agent`` is kept for older tests and
fallback code paths.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse
from src.agents.prompts.router import (
    PROFILE_AGENT_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    SEARCH_AGENT_SYSTEM_PROMPT,
)
from src.infrastructure.database.repositories.estado import (
    EstadoRepository,
    MODE_PROVIDER_PROFILE,
    MODE_PROVIDER_SEARCH,
)
from src.tools.business.providers import (
    ActualizarPrestadorInput,
    BuscarPrestadoresInput,
    ConsultarPrestadorInput,
    CrearPrestadorInput,
    ResolverUbicacionInput,
    RubrosRelacionadosInput,
    actualizar_prestador,
    build_provider_search_report,
    buscar_prestadores,
    buscar_rubros_relacionados,
    consultar_prestador,
    crear_prestador,
    resolver_ubicacion,
)
from src.tools.business.search_state import (
    ConsultarEstadoBusquedaInput,
    GuardarEstadoBusquedaInput,
    LimpiarEstadoBusquedaInput,
    consultar_estado_busqueda,
    guardar_estado_busqueda,
    limpiar_estado_busqueda,
)

logger = logging.getLogger(__name__)

# ── Anti-loop guard ───────────────────────────────────────────────────────────
# Keeps the last tool call signature so we can detect an identical repeated call
# (the LLM re-invokes the same tool with the same params in a loop).
_last_tool_call: dict[str, object] = {}
_last_tool_time: float = 0.0
_ANTI_LOOP_WINDOW_S = 30


class CambiarEstadoConversacionInput(BaseModel):
    """Request a top-level conversation mode switch for the current user."""

    estado: str = Field(
        description=(
            "Nuevo estado principal de la conversación: " "provider_search o provider_profile"
        )
    )


def _build_location_label_from_params(params: object) -> str:
    """Build a location label from barrio and ciudad attributes on the params."""
    barrio = getattr(params, "barrio", None) or ""
    ciudad = getattr(params, "ciudad", None) or ""
    parts = [part for part in [barrio, ciudad] if part]
    return ", ".join(parts) if parts else "la ubicación actual"


def _is_repeat_call(tool_name: str, params: object) -> Any | None:
    """Return a descriptive dict if the exact same call was made recently."""
    global _last_tool_call, _last_tool_time
    now = time.monotonic()
    signature: dict[str, object] = {"tool": tool_name, "params": str(params)}
    if signature == _last_tool_call and now - _last_tool_time < _ANTI_LOOP_WINDOW_S:
        logger.warning(
            "ANTI_LOOP detected repeat call tool=%s params=%s",
            tool_name,
            params,
        )
        location_label = _build_location_label_from_params(params)
        return {
            "info": "duplicate_call_blocked",
            "message": (
                f"Ya buscaste {params.rubro} en {location_label} "
                "y no se encontraron resultados. No tiene sentido repetir la "
                "misma búsqueda. Informale al usuario que no hay resultados y "
                "sugiere alternativas."
            ),
            "rubro": params.rubro,
        }
    _last_tool_call = signature
    _last_tool_time = now
    return None


def _current_metadata(ctx: RunContext[AgentDependencies]) -> dict[str, Any]:
    """Return the injected message metadata or an empty dict."""
    metadata = getattr(ctx.deps, "current_message_metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _current_active_mode(ctx: RunContext[AgentDependencies]) -> str | None:
    """Return the sticky top-level mode injected by the orchestrator."""
    active_mode = _current_metadata(ctx).get("active_mode")
    return active_mode if isinstance(active_mode, str) else None


def _current_agent_name(ctx: RunContext[AgentDependencies]) -> str:
    """Return the agent label injected by the orchestrator for logging."""
    agent_name = _current_metadata(ctx).get("agent_name")
    return agent_name if isinstance(agent_name, str) else "unknown_agent"


def _mode_label(mode: str | None) -> str:
    """Render short human labels for the two top-level conversation modes."""
    if mode == MODE_PROVIDER_PROFILE:
        return "perfil de prestador"
    if mode == MODE_PROVIDER_SEARCH:
        return "búsqueda de servicios"
    return "sin definir"


def _blocked_mode_response(
    *,
    active_mode: str | None,
    required_mode: str,
) -> dict[str, Any]:
    """Explain why a tool is blocked when the chat is in the wrong mode."""
    return {
        "error": "tool_blocked_by_active_mode",
        "active_mode": active_mode,
        "required_mode": required_mode,
        "message": (
            "Esta acción no corresponde al modo activo de la conversación. "
            f"Modo actual: {_mode_label(active_mode)}. "
            f"Modo requerido: {_mode_label(required_mode)}."
        ),
    }


def _ensure_mode(
    ctx: RunContext[AgentDependencies],
    *,
    required_mode: str,
) -> dict[str, Any] | None:
    """Block tool usage when the orchestrator marked another active mode."""
    active_mode = _current_active_mode(ctx)
    if active_mode is None or active_mode == required_mode:
        return None
    logger.info(
        "TOOL_BLOCKED_BY_MODE tool_required=%s active_mode=%s agent=%s user_id=%s",
        required_mode,
        active_mode,
        _current_agent_name(ctx),
        getattr(ctx.deps, "user_id", "?"),
    )
    return _blocked_mode_response(
        active_mode=active_mode,
        required_mode=required_mode,
    )


def _log_tool_call(
    ctx: RunContext[AgentDependencies],
    *,
    tool_name: str,
    params: object,
) -> None:
    """Emit a simple structured log for every tool call."""
    logger.info(
        "AGENT_TOOL_CALL agent=%s mode=%s tool=%s user_id=%s params=%s",
        _current_agent_name(ctx),
        _current_active_mode(ctx),
        tool_name,
        getattr(ctx.deps, "user_id", "?"),
        params,
    )


def _build_agent(system_prompt: str) -> Agent[AgentDependencies, AgentResponse]:
    """Build one stateless agent instance with the supplied system prompt."""
    return Agent(
        model="openai-chat:gpt-4o-mini",
        deps_type=AgentDependencies,
        output_type=AgentResponse,
        system_prompt=system_prompt,
    )


provider_search_agent: Agent[AgentDependencies, AgentResponse] = _build_agent(
    SEARCH_AGENT_SYSTEM_PROMPT
)
provider_profile_agent: Agent[AgentDependencies, AgentResponse] = _build_agent(
    PROFILE_AGENT_SYSTEM_PROMPT
)
router_agent: Agent[AgentDependencies, AgentResponse] = _build_agent(ROUTER_SYSTEM_PROMPT)


async def tool_buscar_prestadores(
    ctx: RunContext[AgentDependencies],
    params: BuscarPrestadoresInput,
) -> dict[str, Any]:
    """Find service providers matching a rubro and optional location."""
    _log_tool_call(ctx, tool_name="tool_buscar_prestadores", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_SEARCH)
    if blocked_mode is not None:
        return build_provider_search_report(
            params,
            [],
            status="wrong_mode",
            message=blocked_mode["message"],
        )

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
            "PROVIDER_ZERO_RESULTS agent=%s rubro=%r barrio=%r ciudad=%r lat=%r lon=%r",
            _current_agent_name(ctx),
            params.rubro,
            params.barrio,
            params.ciudad,
            params.lat,
            params.lon,
        )
    return build_provider_search_report(params, providers)


async def tool_rubros_relacionados(
    ctx: RunContext[AgentDependencies],
    params: RubrosRelacionadosInput,
) -> dict[str, Any]:
    """Return close canonical trade alternatives for a search broadening step."""
    _log_tool_call(ctx, tool_name="tool_rubros_relacionados", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_SEARCH)
    if blocked_mode is not None:
        return blocked_mode
    return await buscar_rubros_relacionados(ctx, params)


async def tool_resolver_ubicacion(
    ctx: RunContext[AgentDependencies],
    params: ResolverUbicacionInput,
) -> dict[str, Any]:
    """Normalize a user-mentioned zone into barrio/ciudad and optional coords."""
    _log_tool_call(ctx, tool_name="tool_resolver_ubicacion", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_SEARCH)
    if blocked_mode is not None:
        return blocked_mode
    return await resolver_ubicacion(ctx, params)


async def tool_consultar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    params: ConsultarEstadoBusquedaInput,
) -> dict[str, Any]:
    """Read the active guided-search state for the current user."""
    _log_tool_call(ctx, tool_name="tool_consultar_estado_busqueda", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_SEARCH)
    if blocked_mode is not None:
        return blocked_mode
    return await consultar_estado_busqueda(ctx, params)


async def tool_guardar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    params: GuardarEstadoBusquedaInput,
) -> dict[str, Any]:
    """Persist the guided-search state for the current user."""
    _log_tool_call(ctx, tool_name="tool_guardar_estado_busqueda", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_SEARCH)
    if blocked_mode is not None:
        return blocked_mode
    return await guardar_estado_busqueda(ctx, params)


async def tool_limpiar_estado_busqueda(
    ctx: RunContext[AgentDependencies],
    params: LimpiarEstadoBusquedaInput,
) -> dict[str, Any]:
    """Clear the guided-search state for the current user."""
    _log_tool_call(ctx, tool_name="tool_limpiar_estado_busqueda", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_SEARCH)
    if blocked_mode is not None:
        return blocked_mode
    return await limpiar_estado_busqueda(ctx, params)


async def tool_crear_prestador(
    ctx: RunContext[AgentDependencies],
    params: CrearPrestadorInput,
) -> dict[str, Any]:
    """Register the current user as a service provider."""
    _log_tool_call(ctx, tool_name="tool_crear_prestador", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_PROFILE)
    if blocked_mode is not None:
        return blocked_mode
    return await crear_prestador(ctx, params)


async def tool_actualizar_prestador(
    ctx: RunContext[AgentDependencies],
    params: ActualizarPrestadorInput,
) -> dict[str, Any]:
    """Update a field in the current user's provider profile."""
    _log_tool_call(ctx, tool_name="tool_actualizar_prestador", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_PROFILE)
    if blocked_mode is not None:
        return blocked_mode
    return await actualizar_prestador(ctx, params)


async def tool_consultar_prestador(
    ctx: RunContext[AgentDependencies],
    params: ConsultarPrestadorInput,
) -> dict[str, Any]:
    """Return the current user's provider profile."""
    _log_tool_call(ctx, tool_name="tool_consultar_prestador", params=params)
    blocked_mode = _ensure_mode(ctx, required_mode=MODE_PROVIDER_PROFILE)
    if blocked_mode is not None:
        return blocked_mode
    return await consultar_prestador(ctx, params)


async def tool_cambiar_estado_conversacion(
    ctx: RunContext[AgentDependencies],
    params: CambiarEstadoConversacionInput,
) -> dict[str, Any]:
    """Persist a top-level mode switch for the current user conversation."""
    _log_tool_call(ctx, tool_name="tool_cambiar_estado_conversacion", params=params)
    if params.estado not in {MODE_PROVIDER_PROFILE, MODE_PROVIDER_SEARCH}:
        return {
            "error": "invalid_mode",
            "message": ("Estado inválido. Usá provider_search o provider_profile."),
        }

    state_repo = EstadoRepository(ctx.deps.db)
    await state_repo.save_mode(ctx.deps.user_id, active_mode=params.estado)

    metadata = _current_metadata(ctx)
    metadata["active_mode"] = params.estado
    metadata["requested_mode_change"] = True
    ctx.deps.current_message_metadata = metadata
    return {
        "updated": True,
        "active_mode": params.estado,
        "message": (f"Listo, cambié el estado principal a {_mode_label(params.estado)}."),
    }


def _register_search_tools(agent: Agent[AgentDependencies, AgentResponse]) -> None:
    """Attach only the search toolset plus the shared mode-switch tool."""
    agent.tool(tool_buscar_prestadores)
    agent.tool(tool_rubros_relacionados)
    agent.tool(tool_resolver_ubicacion)
    agent.tool(tool_consultar_estado_busqueda)
    agent.tool(tool_guardar_estado_busqueda)
    agent.tool(tool_limpiar_estado_busqueda)
    agent.tool(tool_cambiar_estado_conversacion)


def _register_profile_tools(agent: Agent[AgentDependencies, AgentResponse]) -> None:
    """Attach only the provider-profile toolset plus the shared switch tool."""
    agent.tool(tool_crear_prestador)
    agent.tool(tool_actualizar_prestador)
    agent.tool(tool_consultar_prestador)
    agent.tool(tool_cambiar_estado_conversacion)


_register_search_tools(provider_search_agent)
_register_profile_tools(provider_profile_agent)
