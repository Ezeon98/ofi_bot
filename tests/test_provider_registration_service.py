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
        self.assertIn("foto clara del frente de tu DNI", response.message)
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
        self.assertIn("foto clara del frente de tu DNI", response.message)
        state_repo.save.assert_awaited_once_with(
            "5491162527111",
            {
                "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                "paso": "awaiting_dni",
                "nombre": None,
                "edad": None,
                "dni": None,
                "fecha_nacimiento": None,
                "rubros": None,
            },
        )

    async def test_dni_image_is_extracted_and_requires_confirmation(self) -> None:
        """A valid DNI image should move the flow to the confirmation step."""
        expected_age = ProviderRegistrationService._calculate_age("1990-03-20")
        service = ProviderRegistrationService(
            memory_config=SimpleNamespace(enabled=True),
            openai_client=SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(
                        create=AsyncMock(
                            return_value=SimpleNamespace(
                                choices=[
                                    SimpleNamespace(
                                        message=SimpleNamespace(
                                            content=(
                                                '{"document_type":"dni_frente",'
                                                '"is_dni":true,'
                                                '"full_name":"Juan Perez",'
                                                '"dni":"30111222",'
                                                '"birth_date":"1990-03-20"}'
                                            )
                                        )
                                    )
                                ]
                            )
                        )
                    )
                )
            ),
        )
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_dni",
                }
            ),
            save=AsyncMock(),
            delete=AsyncMock(),
        )

        with (
            patch.object(
                provider_registration_service,
                "EstadoRepository",
                return_value=state_repo,
            ),
            patch(
                "builtins.open",
                create=True,
            ) as open_mock,
        ):
            open_mock.return_value.__enter__.return_value.read.return_value = b"fake-image"
            response = await service.maybe_handle_registration(
                user_id="5491162527111",
                message="[imagen]",
                metadata={
                    "message_type": "image",
                    "image_path": "tmp/dni.jpg",
                    "mime_type": "image/jpeg",
                },
                deps=SimpleNamespace(db=SimpleNamespace(scalar=AsyncMock(return_value=None))),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-dni-1",
            )

        self.assertIsNotNone(response)
        self.assertIn("Detecté estos datos en tu DNI", response.message)
        state_repo.save.assert_awaited_once_with(
            "5491162527111",
            {
                "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                "paso": "awaiting_dni_confirmation",
                "nombre": "Juan Perez",
                "edad": expected_age,
                "dni": "30111222",
                "fecha_nacimiento": "1990-03-20",
                "rubros": None,
            },
        )

    async def test_invalid_dni_image_requests_better_photo(self) -> None:
        """Non-DNI images should keep the flow on the DNI photo step."""
        service = ProviderRegistrationService(
            memory_config=SimpleNamespace(enabled=True),
            openai_client=SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(
                        create=AsyncMock(
                            return_value=SimpleNamespace(
                                choices=[
                                    SimpleNamespace(
                                        message=SimpleNamespace(
                                            content=(
                                                '{"document_type":"otro",'
                                                '"is_dni":false,'
                                                '"full_name":null,'
                                                '"dni":null,'
                                                '"birth_date":null}'
                                            )
                                        )
                                    )
                                ]
                            )
                        )
                    )
                )
            ),
        )
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_dni",
                }
            ),
            save=AsyncMock(),
            delete=AsyncMock(),
        )

        with (
            patch.object(
                provider_registration_service,
                "EstadoRepository",
                return_value=state_repo,
            ),
            patch(
                "builtins.open",
                create=True,
            ) as open_mock,
        ):
            open_mock.return_value.__enter__.return_value.read.return_value = b"fake-image"
            response = await service.maybe_handle_registration(
                user_id="5491162527111",
                message="[imagen]",
                metadata={
                    "message_type": "image",
                    "image_path": "tmp/foto.jpg",
                    "mime_type": "image/jpeg",
                },
                deps=SimpleNamespace(db=SimpleNamespace(scalar=AsyncMock(return_value=None))),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-dni-invalid",
            )

        self.assertIsNotNone(response)
        self.assertIn("No pude identificar que la imagen sea el frente de un DNI", response.message)
        state_repo.save.assert_not_awaited()

    async def test_dni_confirmation_yes_moves_to_trades(self) -> None:
        """A positive DNI confirmation should continue directly to rubros."""
        expected_age = ProviderRegistrationService._calculate_age("1990-03-20")
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_dni_confirmation",
                    "nombre": "Juan Perez",
                    "edad": expected_age,
                    "dni": "30111222",
                    "fecha_nacimiento": "1990-03-20",
                }
            ),
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
                message="si",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=SimpleNamespace(scalar=AsyncMock(return_value=None))),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-dni-yes",
            )

        self.assertIsNotNone(response)
        self.assertIn("Contame qué trabajos sabés hacer", response.message)
        state_repo.save.assert_awaited_once_with(
            "5491162527111",
            {
                "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                "paso": "awaiting_trades",
                "nombre": "Juan Perez",
                "edad": expected_age,
                "dni": "30111222",
                "fecha_nacimiento": "1990-03-20",
                "rubros": None,
            },
        )

    async def test_dni_confirmation_no_requests_new_photo(self) -> None:
        """A negative DNI confirmation should ask for another DNI image."""
        expected_age = ProviderRegistrationService._calculate_age("1990-03-20")
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_dni_confirmation",
                    "nombre": "Juan Perez",
                    "edad": expected_age,
                    "dni": "30111222",
                    "fecha_nacimiento": "1990-03-20",
                }
            ),
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
                message="no",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=SimpleNamespace(scalar=AsyncMock(return_value=None))),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-dni-no",
            )

        self.assertIsNotNone(response)
        self.assertIn("foto clara del frente de tu DNI", response.message)
        state_repo.save.assert_awaited_once_with(
            "5491162527111",
            {
                "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                "paso": "awaiting_dni",
                "nombre": None,
                "edad": None,
                "dni": None,
                "fecha_nacimiento": None,
                "rubros": None,
            },
        )

    async def test_trade_description_is_mapped_to_canonical_provider_rubros(self) -> None:
        """The rubro step should persist canonical rubros derived from the reply."""
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_trades",
                    "nombre": "Juan Perez",
                    "edad": 36,
                    "dni": "30111222",
                    "fecha_nacimiento": "1990-03-20",
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
                "edad": 36,
                "dni": "30111222",
                "fecha_nacimiento": "1990-03-20",
                "rubros": [
                    "Plomero",
                    "Electricista",
                    "Técnico en aire acondicionado",
                ],
            },
        )

    async def test_trade_description_accepts_short_provider_self_description(self) -> None:
        """Short replies like 'soy electricista' should still map to canonical rubros."""
        service = ProviderRegistrationService(memory_config=SimpleNamespace(enabled=True))
        state_repo = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "estado": provider_registration_service.REGISTRATION_STATE_NAME,
                    "paso": "awaiting_trades",
                    "nombre": "Juan Perez",
                    "edad": 36,
                    "dni": "30111222",
                    "fecha_nacimiento": "1990-03-20",
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
                message="Soy electricista",
                metadata={"message_type": "text"},
                deps=SimpleNamespace(db=db),
                memory_service=SimpleNamespace(upsert_memory=AsyncMock()),
                turn_id="turn-2b",
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
                "edad": 36,
                "dni": "30111222",
                "fecha_nacimiento": "1990-03-20",
                "rubros": ["Electricista"],
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
                    "dni": "30111222",
                    "fecha_nacimiento": "1990-03-20",
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
        self.assertEqual(response.entities["dni"], "30111222")
        self.assertEqual(response.entities["barrio"], "Caballito")
        state_repo.delete.assert_awaited_once_with("5491162527111")
        memory_service.upsert_memory.assert_awaited_once_with(
            71,
            "provider_registration_age",
            "34",
            0.8,
        )