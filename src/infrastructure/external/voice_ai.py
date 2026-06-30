"""Audio transcription via OpenAI audio transcription API."""

from __future__ import annotations

import logging
from typing import Any

from src.infrastructure.config import get_settings
from src.infrastructure.external.openai_client import build_openai_client

logger = logging.getLogger(__name__)

_PROMPT = (
    "Sos el motor de transcripcion de un bot de WhatsApp de MiOficio. "
    "Transcribi en espanol rioplatense exactamente lo que diga la persona, "
    "priorizando rubros, zonas, direcciones, nombres, telefonos y pedidos de "
    "servicios como plomero, electricista, gasista, flete, limpieza o ninera. "
    "No expliques, no resumas, no respondas: devolve solo la transcripcion."
)

_PROMPT_FRAGMENTS = (
    "sos el motor de transcripcion",
    "transcribi en espanol rioplatense",
    "priorizando rubros, zonas",
    "no expliques, no resumas",
)


def _get_client() -> Any:
    """Build the OpenAI client used for audio transcription."""
    settings = get_settings()
    return build_openai_client(settings)


def _is_hallucinated_prompt(text: str) -> bool:
    """Return True when the model echoed the transcription prompt."""
    lower = text.lower().strip()
    return any(fragment in lower for fragment in _PROMPT_FRAGMENTS)


async def transcribir_audio(archivo: str) -> str:
    """Transcribe an audio file to text using OpenAI."""
    client = _get_client()
    with open(archivo, "rb") as file_handle:
        result = await client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=file_handle,
            language="es",
            prompt=_PROMPT,
        )
    text = result.text.strip()
    if _is_hallucinated_prompt(text):
        logger.warning("Audio transcription matched the injected prompt; dropping result")
        return ""
    return text