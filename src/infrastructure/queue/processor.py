"""Shared webhook message processing logic used by both the FastAPI app and the arq worker."""

from __future__ import annotations

import logging
import os
import time

import redis

from src.infrastructure.config import get_settings
from src.infrastructure.container import UnitOfWork
from src.infrastructure.database.session import get_session_factory
from src.infrastructure.external.whatsapp_client import enviar_mensaje
from src.presentation.bot.handlers.location import reverse_geocode_location
from src.presentation.bot.router import procesar_texto
from src.presentation.bot.terms_gate import (
    POST_TERMS_OFFER_SERVICES_BUTTON_ID,
    POST_TERMS_SEEK_SERVICES_BUTTON_ID,
    handle_terms_gate,
    send_post_terms_service_choice,
)
from src.utils.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Deduplication via Redis ───────────────────────────────────────────────────
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True,
)


def is_duplicate(msg_id: str) -> bool:
    if not msg_id:
        return False
    try:
        result = redis_client.set(f"wa:{msg_id}", 1, nx=True, ex=3600)
        return result is None
    except Exception:
        return False


def is_old_message(message_timestamp: int) -> bool:
    return time.time() - message_timestamp > 600  # 10 min


async def process_webhook_entries(body: dict) -> None:
    """Process all messages in a WhatsApp webhook payload.

    Used by both the inline FastAPI handler and the arq worker.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            uow = UnitOfWork(session)
            await _handle_entries(uow, body)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _handle_entries(uow: UnitOfWork, body: dict) -> None:
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                sender: str = message["from"]
                msg_type: str = message["type"]
                msg_id: str = message.get("id", "")
                msg_timestamp: int = int(message.get("timestamp", "0"))
                bsuid: str | None = message.get("user_id")

                logger.info("Mensaje de %s | tipo: %s", sender, msg_type)

                _HANDLED_TYPES = {"text", "interactive", "location", "audio", "image"}
                if msg_type not in _HANDLED_TYPES:
                    logger.debug("Tipo no manejado ignorado: %s", msg_type)
                    continue

                if is_duplicate(msg_id):
                    logger.info("Mensaje duplicado ignorado: %s", msg_id)
                    continue

                if is_old_message(msg_timestamp):
                    logger.info("Mensaje antiguo ignorado: %s", sender)
                    continue

                if not await check_rate_limit(sender):
                    logger.warning("Rate limit excedido para %s", sender)
                    await enviar_mensaje(
                        sender, "⏳ Estás enviando mensajes muy rápido. Esperá un momento.",
                    )
                    continue

                try:
                    sender, usuario = await uow.usuarios.resolve_sender(sender, bsuid)
                    if not usuario:
                        await uow.usuarios.create(sender, bsuid)
                        usuario = await uow.usuarios.get(sender)

                    await uow.usuarios.touch_interaction(sender)

                    if usuario and await handle_terms_gate(
                        sender=sender,
                        accepted_terms_at=getattr(usuario, "accepted_terms_at", None),
                        message=message,
                        mark_accepted=lambda: uow.usuarios.mark_terms_accepted(sender),
                        on_accept=lambda: send_post_terms_service_choice(sender),
                    ):
                        continue

                    match msg_type:
                        case "text":
                            texto = message["text"]["body"]
                            await procesar_texto(
                                uow,
                                sender,
                                texto,
                                msg_id,
                                metadata={"message_type": "text"},
                            )
                        case "location":
                            loc = message["location"]
                            location_data = await reverse_geocode_location(
                                loc["latitude"],
                                loc["longitude"],
                            )
                            location_parts = [
                                part
                                for part in [location_data.get("barrio"), location_data.get("ciudad")]
                                if part
                            ]
                            location_label = ", ".join(location_parts) or (
                                f"{loc['latitude']}, {loc['longitude']}"
                            )
                            await procesar_texto(
                                uow,
                                sender,
                                f"Mi ubicación es {location_label}",
                                msg_id,
                                metadata={
                                    "message_type": "location",
                                    "latitude": loc["latitude"],
                                    "longitude": loc["longitude"],
                                    "ciudad": location_data.get("ciudad"),
                                    "barrio": location_data.get("barrio"),
                                },
                            )
                        case "interactive":
                            interactive = message.get("interactive", {})
                            int_type = interactive.get("type")
                            if int_type == "list_reply":
                                selected_id = interactive["list_reply"]["id"]
                                title = interactive["list_reply"].get("title", selected_id)
                                await procesar_texto(
                                    uow,
                                    sender,
                                    title,
                                    msg_id,
                                    metadata={
                                        "message_type": "interactive",
                                        "interactive_type": "list_reply",
                                        "selected_id": selected_id,
                                        "selected_title": title,
                                    },
                                )
                            elif int_type == "button_reply":
                                btn_id = interactive["button_reply"]["id"]
                                title = interactive["button_reply"].get("title", btn_id)
                                synthetic_text = title
                                if btn_id == POST_TERMS_SEEK_SERVICES_BUTTON_ID:
                                    synthetic_text = "Quiero buscar servicios"
                                elif btn_id == POST_TERMS_OFFER_SERVICES_BUTTON_ID:
                                    synthetic_text = "Quiero ofrecer servicios"
                                await procesar_texto(
                                    uow,
                                    sender,
                                    synthetic_text,
                                    msg_id,
                                    metadata={
                                        "message_type": "interactive",
                                        "interactive_type": "button_reply",
                                        "button_id": btn_id,
                                        "button_title": title,
                                    },
                                )
                except Exception as exc:
                    logger.exception("Error procesando mensaje de %s", sender)
                    await enviar_mensaje(
                        sender,
                        "❌ Ocurrió un error inesperado. Intentá de nuevo.",
                    )