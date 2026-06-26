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
            entities={"rubro": "electricista", "barrio": "Centro"},
            requires_action=True,
            metadata={
                "providers": [
                    {
                        "nombre": "Electricista Uno",
                        "rubros": ["Electricidad"],
                        "badge_verificado": True,
                        "barrio": "Centro",
                        "telefono": "5491111111111",
                    },
                    {
                        "nombre": "Electricista Dos",
                        "rubros": ["Electricidad"],
                        "barrio": "Centro",
                        "telefono": "5491222222222",
                    },
                ]
            },
        )

        formatted = await service.maybe_reformat_provider_response(response)

        self.assertEqual(formatted.message, "Encontramos 2 electricista que podrían ayudarte en Centro:")
        self.assertEqual(len(formatted.messages), 2)
        self.assertEqual(formatted.messages[0].text.splitlines()[0], "👤 Electricista Uno")
        self.assertEqual(formatted.messages[1].text.splitlines()[0], "👤 Electricista Dos")
        self.assertEqual(formatted.messages[0].action.label, "Contactar")
        self.assertEqual(formatted.messages[1].action.label, "Contactar")


class ProviderSearchServiceShortcutTests(IsolatedAsyncioTestCase):
    """Validate guided-search shortcuts for direct provider requests."""

    async def test_ai_extract_normalizes_non_canonical_air_trade(self) -> None:
        """LLM rubros near the catalog should be remapped before searching."""
        fake_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"rubro": "instalador de aire acondicionado", '
                            '"zona": "Caballito"}'
                        )
                    )
                )
            ]
        )
        openai_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=fake_response)
                )
            )
        )
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
            openai_client=openai_client,
        )

        rubro, zona = await service._ai_extract_rubro_and_zone(
            "Quiero a alguien para instalar un aire en Caballito"
        )

        self.assertEqual(rubro, "Técnico en aire acondicionado")
        self.assertEqual(zona, "Caballito")

    async def test_post_terms_button_starts_search_and_requests_trade_and_zone(self) -> None:
        """The onboarding search button should ask for specialty and zone."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )

        with patch.object(
            provider_search_service,
            "EstadoRepository",
            return_value=SimpleNamespace(
                get=AsyncMock(return_value={}),
                save=AsyncMock(),
                delete=AsyncMock(),
            ),
        ) as state_repo_patch:
            response = await service.maybe_handle_guided_search(
                user_id="5491162527111",
                message="Busco un Servicio",
                metadata={
                    "message_type": "interactive",
                    "button_id": provider_search_service.SEARCH_BUTTON_ID,
                },
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-button-1",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO)
        self.assertIn("qué servicio necesitás", response.message)
        self.assertIn("qué zona", response.message)
        state_repo_patch.return_value.save.assert_awaited_once()

    async def test_awaiting_need_can_use_trade_and_zone_from_same_reply(self) -> None:
        """A follow-up with both rubro and zone should skip the extra zone prompt."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        expected = AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message="Encontré opciones cerca de caballito.",
            confidence=1.0,
            entities={"rubro": "electricista", "barrio": "caballito"},
            requires_action=True,
        )

        with (
            patch.object(
                provider_search_service,
                "EstadoRepository",
                return_value=SimpleNamespace(
                    get=AsyncMock(
                        return_value={
                            "estado": provider_search_service.SEARCH_STATE_NAME,
                            "paso": "awaiting_need",
                        }
                    ),
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
                message="Electricista en Caballito",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-inline-zone",
            )

        self.assertIs(response, expected)
        _, kwargs = build_response_mock.await_args
        self.assertEqual(kwargs["rubro"], "electricista")
        self.assertEqual(kwargs["location"], {"barrio": "caballito"})

    async def test_initial_search_uses_inline_zone_from_buscarme_message(self) -> None:
        """The first search message should reuse the typed zone and skip the agent path."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        expected = AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message="Encontramos 2 electricistas que podrían ayudarte en caballito:",
            messages=[
                Message(text="👤 Maria Electricista"),
                Message(text="👤 Sofia Tecnica"),
            ],
            confidence=1.0,
            entities={"rubro": "electricistas", "barrio": "caballito"},
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
        self.assertEqual(kwargs["location"], {"barrio": "caballito"})

    async def test_problem_statement_with_shared_location_defers_to_agent(self) -> None:
        """Narrative problems should bypass the shortcut so the agent can infer the rubro."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
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
        ):
            response = await service.maybe_handle_guided_search(
                user_id="5491162527111",
                message="En mi casa se rompio un caño de agua",
                metadata={
                    "message_type": "location",
                    "latitude": -34.6205897,
                    "longitude": -58.4413922,
                    "barrio": "Caballito",
                },
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-broken-pipe",
            )

        self.assertIsNone(response)

    async def test_unprefixed_trade_phrase_defers_to_agent(self) -> None:
        """Fresh searches without an explicit search trigger should go to the agent."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
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
        ):
            response = await service.maybe_handle_guided_search(
                user_id="5491162527111",
                message="Electricista por la zona de caballito",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-3",
            )

        self.assertIsNone(response)

    async def test_build_search_results_response_adds_more_prompt_when_pending_results_exist(self) -> None:
        """The first page should include a SI/NO follow-up when more providers remain."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        repo = SimpleNamespace(save=AsyncMock(), delete=AsyncMock())
        providers = [
            {"nombre": "Uno", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "1"},
            {"nombre": "Dos", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "2"},
            {"nombre": "Tres", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "3"},
            {"nombre": "Cuatro", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "4"},
        ]

        with (
            patch.object(provider_search_service, "EstadoRepository", return_value=repo),
            patch.object(
                provider_search_service,
                "buscar_prestadores",
                new=AsyncMock(return_value=providers),
            ),
            patch.object(
                ProviderSearchService,
                "_persist_search_location",
                new=AsyncMock(),
            ),
        ):
            response = await service._build_search_results_response(
                turn_id="turn-more-1",
                deps=SimpleNamespace(
                    db=object(),
                    user_id="5491112345678",
                    usuario_id=71,
                ),
                memory_service=SimpleNamespace(),
                rubro="electricista",
                location={"barrio": "Caballito"},
                detail=None,
            )

        self.assertEqual(
            response.message,
            "Encontramos 3 electricista que podrían ayudarte en Caballito:",
        )
        self.assertEqual(len(response.messages), 4)
        self.assertEqual(response.messages[-1].text, "Queres que te busque mas?")
        self.assertEqual(response.messages[-1].action.type, "reply_buttons")
        self.assertEqual(
            [button.title for button in response.messages[-1].action.buttons],
            ["SI", "NO"],
        )
        repo.save.assert_awaited_once()
        self.assertEqual(len(response.metadata["providers"]), 3)
        self.assertEqual(response.metadata["providers"][0]["nombre"], "Uno")

    async def test_more_results_yes_returns_next_page_without_repeats(self) -> None:
        """Accepting the follow-up should emit the next three pending providers only."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_search_service.SEARCH_STATE_NAME,
                    "paso": "awaiting_more_results",
                    "rubro": "electricista",
                    "barrio": "Caballito",
                    "pending_providers": [
                        {"nombre": "Cuatro", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "4"},
                        {"nombre": "Cinco", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "5"},
                        {"nombre": "Seis", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "6"},
                        {"nombre": "Siete", "rubros": ["Electricista"], "barrio": "Caballito", "telefono": "7"},
                    ],
                }
            ),
            save=AsyncMock(),
            delete=AsyncMock(),
        )

        with patch.object(provider_search_service, "EstadoRepository", return_value=repo):
            response = await service.maybe_handle_guided_search(
                user_id="5491112345678",
                message="SI",
                metadata={
                    "message_type": "interactive",
                    "button_id": provider_search_service.SEARCH_MORE_YES_BUTTON_ID,
                },
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-more-yes",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.message, "Te paso 3 más de electricista en Caballito:")
        self.assertEqual(len(response.messages), 4)
        self.assertEqual(response.messages[0].text.splitlines()[0], "👤 Cuatro")
        self.assertEqual(response.messages[1].text.splitlines()[0], "👤 Cinco")
        self.assertEqual(response.messages[2].text.splitlines()[0], "👤 Seis")
        self.assertEqual(response.messages[3].text, "Queres que te busque mas?")
        repo.save.assert_awaited_once()
        repo.delete.assert_not_awaited()

    async def test_more_results_no_clears_state_and_confirms(self) -> None:
        """Rejecting the follow-up should stop pagination and clear the state."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_search_service.SEARCH_STATE_NAME,
                    "paso": "awaiting_more_results",
                    "rubro": "electricista",
                    "barrio": "Caballito",
                    "pending_providers": [{"nombre": "Cuatro"}],
                }
            ),
            save=AsyncMock(),
            delete=AsyncMock(),
        )

        with patch.object(provider_search_service, "EstadoRepository", return_value=repo):
            response = await service.maybe_handle_guided_search(
                user_id="5491112345678",
                message="NO",
                metadata={
                    "message_type": "interactive",
                    "button_id": provider_search_service.SEARCH_MORE_NO_BUTTON_ID,
                },
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-more-no",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.message, "Entiendo")
        repo.delete.assert_awaited_once_with("5491112345678")

    async def test_awaiting_zone_strips_trade_prefix_from_location_reply(self) -> None:
        """A zone reply like 'electricista en caballito' should keep only the zone."""
        service = ProviderSearchService(
            memory_config=SimpleNamespace(enabled=True),
        )
        expected = AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message="Encontré opciones cerca de caballito.",
            confidence=1.0,
            entities={"rubro": "electricista", "barrio": "caballito"},
            requires_action=True,
        )

        with (
            patch.object(
                provider_search_service,
                "EstadoRepository",
                return_value=SimpleNamespace(
                    get=AsyncMock(
                        return_value={
                            "estado": provider_search_service.SEARCH_STATE_NAME,
                            "paso": "awaiting_zone",
                            "rubro": "electricista",
                        }
                    ),
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
                message="Electricista en Caballito",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=object()),
                memory_service=SimpleNamespace(),
                memories=[],
                turn_id="turn-2",
            )

        self.assertIs(response, expected)
        _, kwargs = build_response_mock.await_args
        self.assertEqual(kwargs["rubro"], "electricista")
        self.assertEqual(kwargs["location"], {"barrio": "caballito"})