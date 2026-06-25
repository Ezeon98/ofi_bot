"""Focused tests for guided provider registration."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.agents.models.response import Intent


def _load_provider_registration_service_module():
    """Import the service with the minimal pydantic_ai stub required by tools."""
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object

    with patch.dict(sys.modules, {"pydantic_ai": fake_pydantic_ai}):
        return import_module("src.application.services.provider_registration_service")


provider_registration_service = _load_provider_registration_service_module()
ProviderRegistrationService = provider_registration_service.ProviderRegistrationService


class ProviderRegistrationServiceTests(IsolatedAsyncioTestCase):
    """Validate the guided onboarding path for provider registration."""

    async def test_post_terms_offer_button_starts_registration_flow(self) -> None:
        """The onboarding offer-services button should open the provider flow."""
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(return_value={}),
            save=AsyncMock(),
            delete=AsyncMock(),
        )

        with patch.object(
            provider_registration_service,
            "EstadoRepository",
            return_value=state_repo,
        ):
            response = await service.maybe_handle_registration(
                user_id="5491162527111",
                message="Quiero ofrecer mis servicios",
                metadata={
                    "message_type": "interactive",
                    "button_id": provider_registration_service.OFFER_SERVICES_BUTTON_ID,
                },
                deps=SimpleNamespace(db=SimpleNamespace(scalar=AsyncMock(return_value=None))),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-offer-button",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.REGISTRAR_PRESTADOR)
        self.assertIn("¿Cómo te llamás?", response.message)
        state_repo.save.assert_awaited_once()

    async def test_registration_intent_starts_flow_and_requests_name(self) -> None:
        """An explicit provider registration request should ask for the name."""
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(return_value={}),
            save=AsyncMock(),
            delete=AsyncMock(),
        )

        with patch.object(
            provider_registration_service,
            "EstadoRepository",
            return_value=state_repo,
        ):
            response = await service.maybe_handle_registration(
                user_id="5491162527111",
                message="Quiero registrarme como proveedor",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=SimpleNamespace(scalar=AsyncMock(return_value=None))),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-1",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.REGISTRAR_PRESTADOR)
        self.assertIn("¿Cómo te llamás?", response.message)
        state_repo.save.assert_awaited_once_with(
            "5491162527111",
            {
                "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                "paso": "awaiting_name",
                "nombre": None,
                "edad": None,
                "rubros": None,
            },
        )

    async def test_trade_description_is_split_into_provider_rubros(self) -> None:
        """The rubro step should persist the services exactly as the provider describes them."""
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_trades",
                    "nombre": "Juan Perez",
                    "edad": 34,
                }
            ),
            save=AsyncMock(),
            delete=AsyncMock(),
        )
        db = SimpleNamespace(
            scalar=AsyncMock(return_value=None),
        )

        with patch.object(
            provider_registration_service,
            "EstadoRepository",
            return_value=state_repo,
        ):
            response = await service.maybe_handle_registration(
                user_id="5491162527111",
                message="Hago plomeria, electricidad y aire acondicionado",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=db),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-2",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.REGISTRAR_PRESTADOR)
        self.assertIn("Ahora decime en qué zona", response.message)
        state_repo.save.assert_awaited_once_with(
            "5491162527111",
            {
                "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                "paso": "awaiting_zone",
                "nombre": "Juan Perez",
                "edad": 34,
                "rubros": [
                    "Plomeria",
                    "Electricidad",
                    "Aire Acondicionado",
                ],
            },
        )

    async def test_zone_reply_creates_provider_and_persists_age_memory(self) -> None:
        """The final zone step should create the provider profile."""
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_zone",
                    "nombre": "Juan Perez",
                    "edad": 34,
                    "rubros": ["Plomeria", "Electricidad"],
                }
            ),
            save=AsyncMock(),
            delete=AsyncMock(),
        )
        memory_service = SimpleNamespace(upsert_memory=AsyncMock())

        with (
            patch.object(
                provider_registration_service,
                "EstadoRepository",
                return_value=state_repo,
            ),
            patch.object(
                provider_registration_service,
                "crear_prestador",
                new=AsyncMock(
                    return_value={
                        "id": 55,
                        "nombre": "Juan Perez",
                        "estado": "pendiente_revision",
                    }
                ),
            ),
            patch.object(
                provider_registration_service,
                "geocode_text_location",
                new=AsyncMock(
                    return_value={
                        "barrio": "Caballito",
                        "ciudad": "CABA",
                        "lat": -34.61,
                        "lon": -58.44,
                    }
                ),
            ),
        ):
            response = await service.maybe_handle_registration(
                user_id="5491162527111",
                message="Caballito",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=object(), user_id="5491162527111", usuario_id=71),
                memory_service=memory_service,
                turn_id="turn-3",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.REGISTRAR_PRESTADOR)
        self.assertIn("ya inicié tu registro como prestador", response.message)
        self.assertEqual(response.entities["edad"], 34)
        self.assertEqual(response.entities["barrio"], "Caballito")
        state_repo.delete.assert_awaited_once_with("5491162527111")
        memory_service.upsert_memory.assert_awaited_once_with(
            71,
            "provider_registration_age",
            "34",
            0.8,
        )