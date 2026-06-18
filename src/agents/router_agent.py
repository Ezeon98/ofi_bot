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

from pydantic_ai import Agent, RunContext

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse
from src.agents.prompts.router import ROUTER_SYSTEM_PROMPT
from src.tools.business.providers import (
    BuscarPrestadoresInput,
    CrearPrestadorInput,
    ActualizarPrestadorInput,
    ConsultarPrestadorInput,
    buscar_prestadores,
    crear_prestador,
    actualizar_prestador,
    consultar_prestador,
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
) -> list[dict]:
    """Find service providers matching a rubro and optional zone."""
    return await buscar_prestadores(ctx, params)


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
