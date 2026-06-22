"""Focused tests for AI-only router dispatch."""

from __future__ import annotations

import sys
from importlib import import_module
from types import SimpleNamespace
from types import ModuleType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.agents.models.response import Intent


def _load_router_module():
    """Import the router with a stubbed AI orchestrator dependency."""
    fake_module = ModuleType("src.orchestrator.ai_orchestrator")

    class DummyAIOrchestrator:  # noqa: D401 - test stub
        """Minimal stub used only to satisfy imports during tests."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def process(self, **_kwargs):
            return SimpleNamespace(
                message="stub",
                intent=Intent.CONVERSACION_GENERAL.value,
                confidence=1.0,
                entities=None,
                requires_action=False,
                metadata=None,
            )

    fake_module.AIOrchestrator = DummyAIOrchestrator

    with patch.dict(sys.modules, {"src.orchestrator.ai_orchestrator": fake_module}):
        return import_module("src.presentation.bot.router")


router = _load_router_module()


class RouterShortcutTests(IsolatedAsyncioTestCase):
    """Validate that inbound messages are always routed to the AI layer."""

    async def test_text_message_always_uses_orchestrator(self) -> None:
        """Even previous shortcut inputs should now go through the orchestrator."""
        orchestrator = SimpleNamespace(
            process=AsyncMock(
                return_value=SimpleNamespace(
                    message="respuesta ai",
                    messages=[],
                    intent=Intent.CONVERSACION_GENERAL.value,
                    confidence=1.0,
                    entities=None,
                    requires_action=False,
                    metadata=None,
                )
            )
        )

        with (
            patch.object(
                router,
                "_get_orchestrator",
                return_value=orchestrator,
            ),
            patch.object(
                router,
                "enviar_typing",
                new=AsyncMock(),
            ),
            patch.object(
                router,
                "enviar_mensaje",
                new=AsyncMock(),
            ) as enviar_mensaje_mock,
        ):
            uow = SimpleNamespace(_session=object())
            await router.procesar_texto(
                uow,
                "5491112345678",
                ".",
                "wamid-1",
                {"message_type": "text"},
            )

        orchestrator.process.assert_awaited_once_with(
            user_id="5491112345678",
            message=".",
            db=uow._session,
            metadata={"message_type": "text"},
        )
        enviar_mensaje_mock.assert_awaited_once_with("5491112345678", "respuesta ai")

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

    async def test_provider_results_send_one_cta_per_message(self) -> None:
        """Provider cards should be delivered without an extra summary text."""
        orchestrator = SimpleNamespace(
            process=AsyncMock(
                return_value=SimpleNamespace(
                    message="Encontré 2 electricistas cerca de Centro:",
                    messages=[
                        {
                            "text": "👤 Electricista Uno\n✅ Verificado",
                            "action": {
                                "type": "cta_url",
                                "label": "Contactar",
                                "url": "https://api.whatsapp.com/send?phone=5491111111111&text=Hola&type=phone_number&app_absent=0",
                            },
                        },
                        {
                            "text": "👤 Electricista Dos\n📍 Centro",
                            "action": {
                                "type": "cta_url",
                                "label": "Contactar",
                                "url": "https://api.whatsapp.com/send?phone=5491222222222&text=Hola&type=phone_number&app_absent=0",
                            },
                        },
                    ],
                    intent=Intent.BUSCAR_SERVICIO.value,
                    confidence=1.0,
                    entities={"rubro": "electricista", "barrio": "Centro"},
                    requires_action=True,
                    metadata=None,
                )
            )
        )

        with (
            patch.object(
                router,
                "_get_orchestrator",
                return_value=orchestrator,
            ),
            patch.object(
                router,
                "enviar_typing",
                new=AsyncMock(),
            ),
            patch.object(
                router,
                "enviar_mensaje",
                new=AsyncMock(),
            ) as enviar_mensaje_mock,
            patch.object(
                router,
                "enviar_boton_cta",
                new=AsyncMock(),
            ) as enviar_boton_cta_mock,
        ):
            uow = SimpleNamespace(_session=object())
            await router.procesar_texto(
                uow,
                "5491112345678",
                "Necesito un electricista",
                "wamid-2",
                {"message_type": "text"},
            )

        enviar_mensaje_mock.assert_not_awaited()
        self.assertEqual(enviar_boton_cta_mock.await_count, 2)