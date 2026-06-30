"""Shared geocoding helpers for location-aware search and messaging."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    from geopy.exc import GeocoderTimedOut
    from geopy.geocoders import Nominatim
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    GeocoderTimedOut = TimeoutError
    Nominatim = None


_geolocator = Nominatim(user_agent="mi-oficio/1.0") if Nominatim is not None else None

# ── TTL cache for geocoding results ─────────────────────────────────────────
# Nominatim has rate limits, so caching is both a latency and a good-citizen win.
_GEOCODE_CACHE_TTL_S = 86400  # 24 h
_reverse_cache: dict[str, tuple[float, dict[str, str]]] = {}
_geocode_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cached_or_compute(
    cache: dict,
    key: str,
    ttl: float,
    compute: callable,
) -> dict:
    """Return cached value if fresh, else compute and store."""
    now = time.monotonic()
    entry = cache.get(key)
    if entry is not None and now - entry[0] < ttl:
        logger.debug("Geocoding cache HIT for key=%s", key)
        return entry[1]
    logger.debug("Geocoding cache MISS for key=%s", key)
    result = compute()
    cache[key] = (now, result)
    return result


def _extract_location_parts(raw: dict[str, Any]) -> dict[str, str]:
    """Normalize useful city/neighborhood-like fields from a geocoder response."""
    return {
        "ciudad": raw.get("city")
        or raw.get("state_district")
        or raw.get("town")
        or raw.get("municipality"),
        "barrio": raw.get("suburb")
        or raw.get("neighbourhood")
        or raw.get("quarter")
        or raw.get("town"),
    }


def _reverse(lat: float, lon: float) -> dict[str, str]:
    if _geolocator is None:
        logger.warning("geopy is not installed; skipping reverse geocoding for (%s, %s)", lat, lon)
        return {}

    cache_key = f"{lat},{lon}"
    return _cached_or_compute(
        _reverse_cache,
        cache_key,
        _GEOCODE_CACHE_TTL_S,
        lambda: _reverse_uncached(lat, lon),
    )


def _reverse_uncached(lat: float, lon: float) -> dict[str, str]:
    """Perform the actual Nominatim reverse call."""
    try:
        location = _geolocator.reverse(f"{lat}, {lon}", language="es", timeout=5)
        if not location:
            return {}
        return _extract_location_parts(location.raw.get("address", {}))
    except GeocoderTimedOut:
        logger.warning("Geocoder timed out for (%s, %s)", lat, lon)
        return {}


def _geocode(query: str) -> dict[str, Any]:
    if _geolocator is None:
        logger.warning("geopy is not installed; skipping text geocoding for '%s'", query)
        return {}

    cache_key = query.strip().lower()
    return _cached_or_compute(
        _geocode_cache,
        cache_key,
        _GEOCODE_CACHE_TTL_S,
        lambda: _geocode_uncached(query),
    )


def _geocode_uncached(query: str) -> dict[str, Any]:
    """Perform the actual Nominatim geocode call."""
    search_query = query if "argentina" in query.lower() else f"{query}, Argentina"
    try:
        location = _geolocator.geocode(search_query, language="es", timeout=5)
        if not location:
            return {}
        parts = _extract_location_parts(location.raw.get("address", {}))
        return {
            **parts,
            "lat": float(location.latitude),
            "lon": float(location.longitude),
            "display_name": location.address,
        }
    except GeocoderTimedOut:
        logger.warning("Geocoder timed out for query '%s'", query)
        return {}


async def reverse_geocode_location(lat: float, lon: float) -> dict[str, str]:
    """Return best-effort city and neighborhood for a coordinate pair."""
    return await asyncio.get_event_loop().run_in_executor(None, _reverse, lat, lon)


async def geocode_text_location(query: str) -> dict[str, Any]:
    """Resolve a neighborhood, city or free-text zone into coordinates."""
    return await asyncio.get_event_loop().run_in_executor(None, _geocode, query)
