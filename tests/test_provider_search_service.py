"""Focused tests for provider-result message formatting."""

from __future__ import annotations

import sys
from importlib import import_module
from types import SimpleNamespace
from types import ModuleType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.agents.models.response import AgentResponse, Intent, Message


def _load_provider_search_service_class():
    """Import the service with the minimal pydantic_ai stub required by tools."""
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object

    with patch.dict(sys.modules, {"pydantic_ai": fake_pydantic_ai}):
        return import_module("src.application.services.provider_search_service")


provider_search_service = _load_provider_search_service_class()
ProviderSearchService = provider_search_service.ProviderSearchService


class ProviderSearchServiceFormattingTests(IsolatedAsyncioTestCase):
    """Lock down one-card-per-provider formatting."""

    async def test_metadata_providers_overrides_collapsed_messages(self) -> None:
        """Multiple providers should always be reformatted into one message each."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        response = AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message="Encontré opciones cerca de Centro.",
            messages=[
                Message(
                    text=(
                        "👤 Electricista Uno\n✅ Verificado\n\n"
                        "👤 Electricista Dos\n📍 Centro"
                    )
                )
            ],
            confidence=1.0,
            entities={"rubro": "electricista", "zona": "Centro"},
            requires_action=True,
            metadata={
                "providers": [
                    {
                        "nombre": "Electricista Uno",
                        "rubros": ["Electricidad"],
                        "badge_verificado": True,
                        "zona": "Centro",
                        "telefono": "5491111111111",
                    },
                    {
                        "nombre": "Electricista Dos",
                        "rubros": ["Electricidad"],
                        "zona": "Centro",
                        "telefono": "5491222222222",
                    },
                ]
            },
        )

        formatted = await service.maybe_reformat_provider_response(response)

        self.assertEqual(formatted.message, "Encontré 2 electricista cerca de Centro:")
        self.assertEqual(len(formatted.messages), 2)
        self.assertEqual(formatted.messages[0].text.splitlines()[0], "👤 Electricista Uno")
        self.assertEqual(formatted.messages[1].text.splitlines()[0], "👤 Electricista Dos")
        self.assertEqual(formatted.messages[0].action.label, "Contactar")
        self.assertEqual(formatted.messages[1].action.label, "Contactar")


class ProviderSearchServiceShortcutTests(IsolatedAsyncioTestCase):
    """Validate guided-search shortcuts for direct provider requests."""

    async def test_initial_search_uses_inline_zone_from_buscarme_message(self) -> None:
        """The first search message should reuse the typed zone and skip the agent path."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        expected = AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message="Encontré 2 electricistas cerca de caballito:",
            messages=[
                Message(text="👤 Maria Electricista"),
                Message(text="👤 Sofia Tecnica"),
            ],
            confidence=1.0,
            entities={"rubro": "electricistas", "zona": "caballito"},
            requires_action=True,
        )

        with (
            patch.object(
                provider_search_service,
                "EstadoRepository",
                return_value=SimpleNamespace(
                    get=AsyncMock(return_value={}),
                    save=AsyncMock(),
                    delete=AsyncMock(),
                ),
            ),
            patch.object(
                ProviderSearchService,
                "_build_search_results_response",
                new=AsyncMock(return_value=expected),
            ) as build_response_mock,
        ):
            response = await service.maybe_handle_guided_search(
                user_id="5491162527111",
                message="Buscarme electricistas en caballito",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-1",
            )

        self.assertIs(response, expected)
        build_response_mock.assert_awaited_once()
        _, kwargs = build_response_mock.await_args
        self.assertEqual(kwargs["rubro"], "electricistas")
        self.assertEqual(kwargs["location"], {"zona": "caballito"})