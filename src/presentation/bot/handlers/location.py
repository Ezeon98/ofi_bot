"""Location handler — reverse-geocodes a WhatsApp location payload."""

from __future__ import annotations

from src.infrastructure.external.whatsapp_client import enviar_mensaje
from src.utils.geocoding import reverse_geocode_location


async def procesar_ubicacion(sender: str, lat: float, lon: float) -> None:
    """Reverse-geocode coordinates and reply to the user."""
    data = await reverse_geocode_location(lat, lon)
    texto = build_location_confirmation(data)
    await enviar_mensaje(sender, texto)


def build_location_confirmation(data: dict[str, str]) -> str:
    """Build a user-facing location confirmation message."""
    ciudad = data.get("ciudad") or "desconocida"
    barrio = data.get("barrio")

    if barrio:
        return f"📍 Recibí tu ubicación. Estás en *{barrio}, {ciudad}*."
    return f"📍 Recibí tu ubicación. Estás en *{ciudad}*."
