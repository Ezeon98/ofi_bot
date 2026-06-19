"""Focused tests for the terms acceptance gate."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.presentation.bot import terms_gate


class TermsGateTests(IsolatedAsyncioTestCase):
    """Validate the onboarding gate before normal bot processing."""

    async def test_send_terms_prompt_sends_document_before_buttons(self) -> None:
        """The prompt should attach the PDF when the terms file exists."""
        with TemporaryDirectory() as temp_dir:
            terms_file = Path(temp_dir) / "terms.pdf"
            terms_file.write_bytes(b"%PDF-1.4\n")

            with (
                patch.object(
                    terms_gate,
                    "TERMS_FILE_PATH",
                    terms_file,
                ),
                patch.object(
                    terms_gate,
                    "enviar_documento",
                    new=AsyncMock(),
                ) as enviar_documento_mock,
                patch.object(
                    terms_gate,
                    "enviar_botones_respuesta",
                    new=AsyncMock(),
                ) as enviar_botones_mock,
            ):
                await terms_gate.send_terms_prompt("5491112345678")

        enviar_documento_mock.assert_awaited_once_with(
            "5491112345678",
            str(terms_file),
            "terms.pdf",
            caption="Términos y condiciones de LaburáYA.",
        )
        enviar_botones_mock.assert_awaited_once()

    async def test_accept_button_marks_acceptance_and_continues_onboarding(self) -> None:
        """Accepting terms should persist acceptance and continue onboarding."""
        uow = SimpleNamespace(usuarios=SimpleNamespace(mark_terms_accepted=AsyncMock()))
        message = {
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": terms_gate.TERMS_ACCEPT_BUTTON_ID},
            }
        }

        with (
            patch.object(
                terms_gate,
                "enviar_mensaje",
                new=AsyncMock(),
            ) as enviar_mensaje_mock,
            patch.object(
                terms_gate,
                "send_terms_prompt",
                new=AsyncMock(),
            ) as enviar_terminos_mock,
        ):
            on_accept = AsyncMock()
            handled = await terms_gate.handle_terms_gate(
                sender="5491112345678",
                accepted_terms_at=None,
                message=message,
                mark_accepted=uow.usuarios.mark_terms_accepted,
                on_accept=on_accept,
            )

        self.assertTrue(handled)
        uow.usuarios.mark_terms_accepted.assert_awaited_once_with()
        enviar_mensaje_mock.assert_awaited_once()
        on_accept.assert_awaited_once_with()
        enviar_terminos_mock.assert_not_awaited()

    async def test_send_post_terms_service_choice_sends_expected_buttons(self) -> None:
        """The post-acceptance step should ask whether the user offers or seeks services."""
        with patch.object(
            terms_gate,
            "enviar_botones_respuesta",
            new=AsyncMock(),
        ) as enviar_botones_mock:
            await terms_gate.send_post_terms_service_choice("5491112345678")

        enviar_botones_mock.assert_awaited_once_with(
            "5491112345678",
            "Gracias. Registramos tu aceptación. ¿Ofrecés servicios o buscás servicios?",
            [
                {
                    "id": terms_gate.POST_TERMS_OFFER_SERVICES_BUTTON_ID,
                    "title": "Ofrezco servicios",
                },
                {
                    "id": terms_gate.POST_TERMS_SEEK_SERVICES_BUTTON_ID,
                    "title": "Busco servicios",
                },
            ],
        )

    async def test_plain_message_without_acceptance_sends_terms_prompt(self) -> None:
        """Any non-accept message should resend the terms prompt."""
        uow = SimpleNamespace(usuarios=SimpleNamespace(mark_terms_accepted=AsyncMock()))
        message = {"type": "text", "text": {"body": "hola"}}

        with (
            patch.object(
                terms_gate,
                "enviar_mensaje",
                new=AsyncMock(),
            ) as enviar_mensaje_mock,
            patch.object(
                terms_gate,
                "send_terms_prompt",
                new=AsyncMock(),
            ) as enviar_terminos_mock,
        ):
            on_accept = AsyncMock()
            handled = await terms_gate.handle_terms_gate(
                sender="5491112345678",
                accepted_terms_at=None,
                message=message,
                mark_accepted=uow.usuarios.mark_terms_accepted,
                on_accept=on_accept,
            )

        self.assertTrue(handled)
        uow.usuarios.mark_terms_accepted.assert_not_awaited()
        enviar_mensaje_mock.assert_not_awaited()
        on_accept.assert_not_awaited()
        enviar_terminos_mock.assert_awaited_once_with("5491112345678")