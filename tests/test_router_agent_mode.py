"""Focused tests for mode-gated router-agent tools."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch


class _FakeAgent:
    """Tiny stand-in for pydantic_ai.Agent used during import."""

    def __init__(self, *args, **kwargs) -> None:
        """Accept the same constructor shape as the real agent."""

    def tool(self, func):
        """Return the wrapped function unchanged."""
        return func


def _load_router_agent_module():
    """Import router_agent with a minimal pydantic_ai stub."""
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.Agent = _FakeAgent
    fake_pydantic_ai.RunContext = object
    sys.modules.setdefault("pydantic_ai", fake_pydantic_ai)
    return import_module("src.agents.router_agent")


router_agent_module = _load_router_agent_module()
provider_tools = import_module("src.tools.business.providers")
search_state_tools = import_module("src.tools.business.search_state")
router_prompt_module = import_module("src.agents.prompts.router")


class RouterAgentModeTests(IsolatedAsyncioTestCase):
    """Validate that router-agent tools respect the orchestrator mode."""

    def _ctx(self, active_mode: str | None) -> SimpleNamespace:
        """Build a minimal RunContext-like object with injected metadata."""
        return SimpleNamespace(
            deps=SimpleNamespace(
                db=object(),
                user_id="5491112345678",
                current_message_metadata=(
                    {"active_mode": active_mode} if active_mode is not None else {}
                ),
            )
        )

    async def test_search_tool_is_blocked_in_profile_mode(self) -> None:
        """Provider-profile mode should reject provider search tools."""
        params = provider_tools.BuscarPrestadoresInput(
            rubro="plomero",
            barrio="Caballito",
        )

        with patch.object(
            router_agent_module,
            "buscar_prestadores",
            new=AsyncMock(return_value=[]),
        ) as search_mock:
            result = await router_agent_module.tool_buscar_prestadores(
                self._ctx(router_agent_module.MODE_PROVIDER_PROFILE),
                params,
            )

        self.assertEqual(result["status"], "wrong_mode")
        self.assertIn("perfil de prestador", result["message"])
        search_mock.assert_not_awaited()

    async def test_profile_tool_is_blocked_in_search_mode(self) -> None:
        """Search mode should reject provider profile tools."""
        params = provider_tools.ConsultarPrestadorInput()

        with patch.object(
            router_agent_module,
            "consultar_prestador",
            new=AsyncMock(return_value={"id": 1}),
        ) as consult_mock:
            result = await router_agent_module.tool_consultar_prestador(
                self._ctx(router_agent_module.MODE_PROVIDER_SEARCH),
                params,
            )

        self.assertEqual(result["error"], "tool_blocked_by_active_mode")
        self.assertEqual(
            result["required_mode"],
            router_agent_module.MODE_PROVIDER_PROFILE,
        )
        consult_mock.assert_not_awaited()

    async def test_search_state_tool_runs_inside_search_mode(self) -> None:
        """Search-only state tools should stay available in search mode."""
        params = search_state_tools.ConsultarEstadoBusquedaInput()

        with patch.object(
            router_agent_module,
            "consultar_estado_busqueda",
            new=AsyncMock(return_value={"activa": True}),
        ) as state_mock:
            result = await router_agent_module.tool_consultar_estado_busqueda(
                self._ctx(router_agent_module.MODE_PROVIDER_SEARCH),
                params,
            )

        self.assertEqual(result, {"activa": True})
        state_mock.assert_awaited_once()

    async def test_state_switch_tool_persists_new_top_level_mode(self) -> None:
        """Both agents should have a shared tool that persists the active mode."""
        fake_state_repo = SimpleNamespace(save_mode=AsyncMock())

        with patch.object(
            router_agent_module,
            "EstadoRepository",
            return_value=fake_state_repo,
        ):
            result = await router_agent_module.tool_cambiar_estado_conversacion(
                self._ctx(router_agent_module.MODE_PROVIDER_SEARCH),
                router_agent_module.CambiarEstadoConversacionInput(
                    estado=router_agent_module.MODE_PROVIDER_PROFILE,
                ),
            )

        self.assertTrue(result["updated"])
        self.assertEqual(
            result["active_mode"],
            router_agent_module.MODE_PROVIDER_PROFILE,
        )
        fake_state_repo.save_mode.assert_awaited_once_with(
            "5491112345678",
            active_mode=router_agent_module.MODE_PROVIDER_PROFILE,
        )


def test_router_module_exports_two_specialized_agents() -> None:
    """Search and provider-profile flows should have distinct agent instances."""
    assert router_agent_module.provider_search_agent is not router_agent_module.provider_profile_agent


def test_router_prompt_mentions_active_mode_constraints() -> None:
    """The search prompt should describe its active mode and restricted tools."""
    prompt = router_prompt_module.ROUTER_SYSTEM_PROMPT

    assert "active_mode=provider_search" in prompt
    assert "tool_cambiar_estado_conversacion" in prompt
    assert "tool_buscar_prestadores" in prompt


def test_specialized_prompts_mention_state_switch_tool() -> None:
    """Both specialized prompts should expose the state-switch tool explicitly."""
    assert "tool_cambiar_estado_conversacion" in router_prompt_module.SEARCH_AGENT_SYSTEM_PROMPT
    assert "tool_cambiar_estado_conversacion" in router_prompt_module.PROFILE_AGENT_SYSTEM_PROMPT
    assert "active_mode=provider_profile" in router_prompt_module.PROFILE_AGENT_SYSTEM_PROMPT
    assert "tool_crear_prestador" in router_prompt_module.PROFILE_AGENT_SYSTEM_PROMPT
