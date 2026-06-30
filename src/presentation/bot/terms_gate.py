"""Terms acceptance gate for the WhatsApp onboarding flow."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from src.infrastructure.external.whatsapp_client import (
    enviar_botones_respuesta,
    enviar_documento,
    enviar_mensaje,
)

logger = logging.getLogger(__name__)

TERMS_FILE_PATH = (
    Path(__file__).resolve().parents[3] / "static" / "legal" / "mioficio-terminos-y-condiciones.pdf"
)
TERMS_ACCEPT_BUTTON_ID = "terms_accept"
TERMS_REJECT_BUTTON_ID = "terms_reject"
POST_TERMS_OFFER_SERVICES_BUTTON_ID = "post_terms_offer_services"
POST_TERMS_SEEK_SERVICES_BUTTON_ID = "post_terms_seek_services"


def button_reply_id(message: dict) -> str | None:
    """Extract a WhatsApp button reply id when present."""
    interactive = message.get("interactive")
    if not isinstance(interactive, dict) or interactive.get("type") != "button_reply":
        return None
    button_reply = interactive.get("button_reply")
    if not isinstance(button_reply, dict):
        return None
    button_id = button_reply.get("id")
    return button_id if isinstance(button_id, str) else None


async def send_terms_prompt(sender: str) -> None:
    """Send the terms PDF and acceptance buttons."""
    if TERMS_FILE_PATH.exists():
        await enviar_documento(
            sender,
            str(TERMS_FILE_PATH),
            TERMS_FILE_PATH.name,
            caption="Términos y condiciones de MiOficio.",
        )
    else:
        logger.warning("Terms file not found: %s", TERMS_FILE_PATH)

    await enviar_botones_respuesta(
        sender,
        "Para continuar, leé el archivo y elegí una opción.",
        [
            {"id": TERMS_ACCEPT_BUTTON_ID, "title": "Aceptar"},
            {"id": TERMS_REJECT_BUTTON_ID, "title": "Rechazar"},
        ],
    )


async def send_post_terms_service_choice(sender: str) -> None:
    """Ask the user whether they offer or seek services."""
    await enviar_botones_respuesta(
        sender,
        "Gracias. Registramos tu aceptación. ¿Qué querés hacer ahora?",
        [
            {
                "id": POST_TERMS_OFFER_SERVICES_BUTTON_ID,
                "title": "Ofrecer servicios",
            },
            {
                "id": POST_TERMS_SEEK_SERVICES_BUTTON_ID,
                "title": "Buscar servicios",
            },
        ],
    )


async def handle_terms_gate(
    sender: str,
    accepted_terms_at: datetime | None,
    message: dict,
    mark_accepted: Callable[[], Awaitable[None]],
    on_accept: Callable[[], Awaitable[None]],
) -> bool:
    """Block normal bot flow until the user accepts the terms."""
    if accepted_terms_at is not None:
        return False

    selected_button_id = button_reply_id(message)
    if selected_button_id == TERMS_ACCEPT_BUTTON_ID:
        await mark_accepted()
        await on_accept()
        return True

    if selected_button_id == TERMS_REJECT_BUTTON_ID:
        await enviar_mensaje(
            sender,
            "Para usar MiOficio necesitás aceptar los términos y condiciones.",
        )

    await send_terms_prompt(sender)
    return True
