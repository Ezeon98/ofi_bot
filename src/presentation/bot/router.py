"""Bot router — dispatches messages to the AI orchestrator."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.infrastructure.config import get_settings
from src.infrastructure.container import UnitOfWork
from src.infrastructure.external.whatsapp_client import (
    enviar_boton_cta,
    enviar_botones_respuesta,
    enviar_mensaje,
    enviar_typing,
)
from src.orchestrator.ai_orchestrator import AIOrchestrator
from src.utils.agent_logger import AgentLogger

logger = logging.getLogger(__name__)

# Singleton orchestrator — built once, reused per message.
# ponytail: module-level singleton; fine for single-process deployments.
#           For multi-worker setups pass via DI container instead.
_orchestrator: AIOrchestrator | None = None
_alog = AgentLogger(enabled=get_settings().agent_logging_enabled)


def _get_orchestrator() -> AIOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AIOrchestrator(get_settings())
    return _orchestrator


async def procesar_texto(
    uow: UnitOfWork,
    sender: str,
    texto: str,
    message_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Process an incoming user message via the AI orchestrator."""
    _start = time.monotonic()
    logger.info("INCOMING user=%s preview=%s", sender, texto[:80])

    if not get_settings().ai_enabled:
        await enviar_mensaje(sender, f"Recibido: {texto}")
        return

    orchestrator = _get_orchestrator()
    await enviar_typing(sender, message_id)

    _alog.info("", "router.process", user_id=sender, message_length=len(texto))
    response = await orchestrator.process(
        user_id=sender,
        message=texto,
        db=uow._session,
        metadata=metadata,
    )
    elapsed_ms = (time.monotonic() - _start) * 1000

    logger.info(
        "OUTGOING user=%s source=%s intent=%s confidence=%.2f elapsed=%.0fms msg_len=%d",
        sender,
        response.source,
        response.intent,
        response.confidence,
        elapsed_ms,
        len(response.message),
    )
    _alog.info(
        "", "router.response",
        user_id=sender,
        source=response.source,
        intent=response.intent,
        confidence=response.confidence,
        requires_action=response.requires_action,
        elapsed_ms=round(elapsed_ms, 1),
        response_preview=response.message[:120],
    )
    if response.messages:
        if response.message:
            await enviar_mensaje(sender, response.message)
        await _send_additional_messages(sender, response.messages)
        return

    await enviar_mensaje(sender, response.message)


async def _send_additional_messages(
    sender: str,
    messages: list[dict[str, Any]],
) -> None:
    """Send each additional outbound message, preserving CTA buttons."""
    for msg in messages:
        text = msg.get("text", "")
        action = msg.get("action")
        if action and action.get("type") == "cta_url":
            await enviar_boton_cta(
                sender,
                body_text=text,
                display_text=action["label"],
                url=action["url"],
            )
        elif action and action.get("type") == "reply_buttons":
            await enviar_botones_respuesta(
                sender,
                body_text=text,
                buttons=action.get("buttons") or [],
            )
        else:
            await enviar_mensaje(sender, text)
