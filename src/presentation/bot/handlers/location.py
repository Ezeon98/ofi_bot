"""Location handler — reverse-geocodes a WhatsApp location payload."""

from __future__ import annotations

import asyncio
import logging

from geopy.exc import GeocoderTimedOut
from geopy.geocoders import Nominatim

from src.infrastructure.external.whatsapp_client import enviar_mensaje

logger = logging.getLogger(__name__)

# ponytail: module-level geolocator; Nominatim rate-limits to 1 req/s.
#           For higher throughput swap to a paid provider (e.g. geocoder.us).
_geolocator = Nominatim(user_agent="mi-oficio/1.0")


def _reverse(lat: float, lon: float) -> dict:
    try:
        location = _geolocator.reverse(f"{lat}, {lon}", language="es", timeout=5)
        if not location:
            return {}
        raw = location.raw.get("address", {})
        return {
            "ciudad": raw.get("state_district") or raw.get("town") or raw.get("municipality"),
            "barrio": raw.get("town") or raw.get("neighbourhood") or raw.get("quarter"),
        }
    except GeocoderTimedOut:
        logger.warning("Geocoder timed out for (%s, %s)", lat, lon)
        return {}


async def procesar_ubicacion(sender: str, lat: float, lon: float) -> None:
    """Reverse-geocode coordinates and reply to the user."""
    # geopy is sync — run in threadpool to avoid blocking the event loop
    data = await asyncio.get_event_loop().run_in_executor(None, _reverse, lat, lon)

    ciudad = data.get("ciudad") or "desconocida"
    barrio = data.get("barrio")

    if barrio:
        texto = f"📍 Recibí tu ubicación. Estás en *{barrio}, {ciudad}*."
    else:
        texto = f"📍 Recibí tu ubicación. Estás en *{ciudad}*."

    await enviar_mensaje(sender, texto)
