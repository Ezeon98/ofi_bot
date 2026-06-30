"""Focused tests for inbound audio processing."""

from __future__ import annotations

import sys
from importlib import import_module
from types import SimpleNamespace
from types import ModuleType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch


def _load_processor_module():
    """Import the processor with stubbed runtime-heavy dependencies."""
    fake_redis = ModuleType("redis")
    fake_router = ModuleType("src.presentation.bot.router")
    fake_location = ModuleType("src.presentation.bot.handlers.location")
    fake_terms_gate = ModuleType("src.presentation.bot.terms_gate")

    class DummyRedis:  # noqa: D401 - test stub
        """Minimal stub used only to satisfy imports during tests."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def set(self, *_args, **_kwargs):
            return True

    fake_redis.Redis = DummyRedis
    fake_router.procesar_texto = AsyncMock()
    fake_location.reverse_geocode_location = AsyncMock(return_value={})
    fake_terms_gate.POST_TERMS_OFFER_SERVICES_BUTTON_ID = "offer-services"
    fake_terms_gate.POST_TERMS_SEEK_SERVICES_BUTTON_ID = "seek-services"
    fake_terms_gate.handle_terms_gate = AsyncMock(return_value=False)
    fake_terms_gate.send_post_terms_service_choice = AsyncMock()

    with patch.dict(
        sys.modules,
        {
            "redis": fake_redis,
            "src.presentation.bot.router": fake_router,
            "src.presentation.bot.handlers.location": fake_location,
            "src.presentation.bot.terms_gate": fake_terms_gate,
        },
    ):
        return import_module("src.infrastructure.queue.processor")


processor = _load_processor_module()


class AudioProcessorTests(IsolatedAsyncioTestCase):
    """Validate the webhook audio flow before the AI router."""

    def test_build_location_message_text_uses_coordinates(self) -> None:
        """Shared current location should be normalized into coordinate text."""
        self.assertEqual(
            processor._build_location_message_text(-34.6205897, -58.4413922),
            "Mi ubicación es -34.6205897, -58.4413922",
        )

    async def test_audio_message_routes_transcribed_text(self) -> None:
        """A valid audio note should be transcribed and processed as plain text."""
        uow = SimpleNamespace()
        message = {"audio": {"id": "media-123"}}

        with (
            patch.object(processor, "descargar_media", new=AsyncMock()),
            patch.object(processor, "get_audio_duration", return_value=12.0),
            patch.object(
                processor,
                "transcribir_audio",
                new=AsyncMock(return_value="Necesito un plomero en Caballito"),
            ),
            patch.object(processor, "procesar_texto", new=AsyncMock()) as procesar_texto_mock,
            patch.object(processor, "enviar_mensaje", new=AsyncMock()) as enviar_mensaje_mock,
            patch.object(processor, "os") as os_mock,
        ):
            os_mock.path.join.return_value = "tmp/audio.ogg"
            os_mock.path.exists.return_value = True

            await processor._process_audio_message(
                uow,
                "5491112345678",
                message,
                "wamid-audio-1",
            )

        procesar_texto_mock.assert_awaited_once_with(
            uow,
            "5491112345678",
            "Necesito un plomero en Caballito",
            "wamid-audio-1",
            metadata={"message_type": "audio", "media_id": "media-123"},
        )
        enviar_mensaje_mock.assert_not_awaited()
        os_mock.remove.assert_called_once_with("tmp/audio.ogg")

    async def test_audio_message_over_limit_is_rejected(self) -> None:
        """Audio longer than 30 seconds should not reach the text router."""
        uow = SimpleNamespace()
        message = {"audio": {"id": "media-456"}}

        with (
            patch.object(processor, "descargar_media", new=AsyncMock()),
            patch.object(
                processor,
                "get_audio_duration",
                return_value=processor.AUDIO_MAX_SECONDS + 1,
            ),
            patch.object(processor, "transcribir_audio", new=AsyncMock()) as transcribir_audio_mock,
            patch.object(processor, "procesar_texto", new=AsyncMock()) as procesar_texto_mock,
            patch.object(processor, "enviar_mensaje", new=AsyncMock()) as enviar_mensaje_mock,
            patch.object(processor, "os") as os_mock,
        ):
            os_mock.path.join.return_value = "tmp/audio.ogg"
            os_mock.path.exists.return_value = True

            await processor._process_audio_message(
                uow,
                "5491112345678",
                message,
                "wamid-audio-2",
            )

        procesar_texto_mock.assert_not_awaited()
        transcribir_audio_mock.assert_not_awaited()
        enviar_mensaje_mock.assert_awaited_once()
        os_mock.remove.assert_called_once_with("tmp/audio.ogg")

    async def test_post_terms_search_button_routes_requested_mode_metadata(self) -> None:
        """The post-terms search button should pass explicit mode metadata."""
        uow = SimpleNamespace(
            usuarios=SimpleNamespace(
                resolve_sender=AsyncMock(
                    return_value=(
                        "5491112345678",
                        SimpleNamespace(accepted_terms_at="2026-01-01"),
                    )
                ),
                touch_interaction=AsyncMock(),
            )
        )
        body = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "5491112345678",
                                        "type": "interactive",
                                        "id": "wamid-interactive-1",
                                        "timestamp": "9999999999",
                                        "interactive": {
                                            "type": "button_reply",
                                            "button_reply": {
                                                "id": processor.POST_TERMS_SEEK_SERVICES_BUTTON_ID,
                                                "title": "Buscar servicios",
                                            },
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        with (
            patch.object(processor, "is_duplicate", return_value=False),
            patch.object(processor, "is_old_message", return_value=False),
            patch.object(processor, "check_rate_limit", new=AsyncMock(return_value=True)),
            patch.object(processor, "handle_terms_gate", new=AsyncMock(return_value=False)),
            patch.object(processor, "procesar_texto", new=AsyncMock()) as procesar_texto_mock,
        ):
            await processor._handle_entries(uow, body)

        procesar_texto_mock.assert_awaited_once_with(
            uow,
            "5491112345678",
            "Buscar servicios",
            "wamid-interactive-1",
            metadata={
                "message_type": "interactive",
                "interactive_type": "button_reply",
                "selected_id": processor.POST_TERMS_SEEK_SERVICES_BUTTON_ID,
                "selected_title": "Buscar servicios",
                "button_id": processor.POST_TERMS_SEEK_SERVICES_BUTTON_ID,
                "button_title": "Buscar servicios",
                "requested_mode": "provider_search",
                "mode_source": "post_terms_button",
            },
        )