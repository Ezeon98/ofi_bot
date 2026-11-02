"""Helpers to fan out admin notifications to all configured admins."""

from __future__ import annotations

from src.infrastructure.config import get_settings
from src.infrastructure.external.whatsapp_client import (
    enviar_botones_respuesta,
    enviar_documento,
    enviar_imagen,
    enviar_mensaje,
)


def get_admin_phones() -> list[str]:
    """Return the configured admin recipients."""
    return get_settings().admin_phones


async def send_admin_message(message: str) -> None:
    """Send a text notification to every configured admin."""
    for phone in get_admin_phones():
        await enviar_mensaje(phone, message)


async def send_admin_image(image_path: str, *, caption: str = "") -> None:
    """Send an image notification to every configured admin."""
    for phone in get_admin_phones():
        await enviar_imagen(phone, image_path, caption=caption)


async def send_admin_document(
    document_path: str,
    filename: str,
    *,
    caption: str = "",
) -> None:
    """Send a document notification to every configured admin."""
    for phone in get_admin_phones():
        await enviar_documento(phone, document_path, filename, caption=caption)


async def send_admin_buttons(
    message: str,
    buttons: list[dict[str, str]],
) -> None:
    """Send a button prompt to every configured admin."""
    for phone in get_admin_phones():
        await enviar_botones_respuesta(phone, message, buttons)
