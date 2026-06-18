"""Focused tests for text-router shortcuts."""

from __future__ import annotations

import sys
from importlib import import_module
from types import SimpleNamespace
from types import ModuleType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch


def _load_router_module():
    """Import the router with a stubbed AI orchestrator dependency."""
    fake_module = ModuleType("src.orchestrator.ai_orchestrator")

    class DummyAIOrchestrator:  # noqa: D401 - test stub
        """Minimal stub used only to satisfy imports during tests."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def process(self, **_kwargs):
            return SimpleNamespace(message="stub")

    fake_module.AIOrchestrator = DummyAIOrchestrator

    with patch.dict(sys.modules, {"src.orchestrator.ai_orchestrator": fake_module}):
        return import_module("src.presentation.bot.router")


router = _load_router_module()


class RouterShortcutTests(IsolatedAsyncioTestCase):
    """Validate hard-coded shortcuts that bypass the AI layer."""

    async def test_dot_shortcut_sends_mock_cards(self) -> None:
        """A single dot should trigger the mock card preview."""
        with (
            patch.object(
                router,
                "enviar_cards_mock",
                new=AsyncMock(),
            ) as enviar_cards_mock,
            patch.object(
                router,
                "_get_orchestrator",
            ) as get_orchestrator,
        ):
            await router.procesar_texto(SimpleNamespace(), "5491112345678", ".")

        enviar_cards_mock.assert_awaited_once_with("5491112345678")
        get_orchestrator.assert_not_called()

    async def test_cards_handler_sends_one_cta_per_mock_card(self) -> None:
        """The mock handler should emit the expected intro plus CTA cards."""
        from src.presentation.bot.handlers import mock_cards

        with (
            patch.object(
                mock_cards,
                "enviar_mensaje",
                new=AsyncMock(),
            ) as enviar_mensaje,
            patch.object(
                mock_cards,
                "enviar_boton_cta",
                new=AsyncMock(),
            ) as enviar_boton_cta,
        ):
            await mock_cards.enviar_cards_mock("5491112345678")

        enviar_mensaje.assert_awaited_once()
        self.assertEqual(enviar_boton_cta.await_count, 3)