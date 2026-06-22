"""Mock card-like responses for WhatsApp manual testing."""

from __future__ import annotations

from src.infrastructure.external.whatsapp_client import (
    build_whatsapp_contact_url,
    enviar_boton_cta,
    enviar_mensaje,
)

_MOCK_CARDS = [
    {
        "name": "Francisco Alejandro Verón",
        "status": "Matrícula en proceso",
        "provincial": True,
        "national": True,
        "phone": "5493875550101",
    },
    {
        "name": "Rodríguez Esteban Adrián",
        "status": "Matrícula en proceso",
        "provincial": True,
        "national": True,
        "phone": "5493875550102",
    },
    {
        "name": "Pablo Acosta",
        "status": "Matrícula validada",
        "provincial": True,
        "national": False,
        "phone": "5493875550103",
    },
]


def _build_card_body(card: dict[str, object]) -> str:
    """Render a provider card as a compact WhatsApp-friendly text block."""
    provincial = (
        "✅ Antecedentes provinciales presentados."
        if card["provincial"]
        else "⌛ Antecedentes provinciales pendientes."
    )
    national = (
        "✅ Antecedentes nacionales presentados."
        if card["national"]
        else "⌛ Antecedentes nacionales pendientes."
    )
    return (
        f"👤 {card['name']}\n"
        f"🏅 {card['status']}\n"
        f"{provincial}\n"
        f"{national}"
    )


def _build_contact_url(card: dict[str, object]) -> str:
    """Build a WhatsApp deeplink for the mock contact CTA."""
    return build_whatsapp_contact_url(
        str(card["phone"]),
        f"Hola {card['name']}, te contacto desde LaburáYA.",
    )


async def enviar_cards_mock(sender: str) -> None:
    """Send a short sequence of mock provider cards for UI testing."""
    await enviar_mensaje(
        sender,
        "🧪 Test visual: te mando una versión mockeada tipo card.",
    )
    for card in _MOCK_CARDS:
        await enviar_boton_cta(
            sender,
            _build_card_body(card),
            "Contactar",
            _build_contact_url(card),
        )