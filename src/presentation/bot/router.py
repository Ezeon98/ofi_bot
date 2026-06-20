"""Bot router — dispatches messages to the AI orchestrator."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.infrastructure.config import get_settings
from src.infrastructure.container import UnitOfWork
from src.infrastructure.external.whatsapp_client import enviar_mensaje, enviar_typing, enviar_boton_cta
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
        "OUTGOING user=%s intent=%s confidence=%.2f elapsed=%.0fms msg_len=%d",
        sender,
        response.intent,
        response.confidence,
        elapsed_ms,
        len(response.message),
    )
    _alog.info(
        "", "router.response",
        user_id=sender,
        intent=response.intent,
        confidence=response.confidence,
        requires_action=response.requires_action,
        elapsed_ms=round(elapsed_ms, 1),
        response_preview=response.message[:120],
    )
    # ── Send the primary message ───────────────────────────────────────
    await enviar_mensaje(sender, response.message)

    # ── Send additional messages (one per provider with optional contact button) ──
    for msg in response.messages:
        text = msg.get("text", "")
        action = msg.get("action")
        if action and action.get("type") == "cta_url":
            await enviar_boton_cta(
                sender,
                body_text=text,
                display_text=action["label"],
                url=action["url"],
            )
        else:
            await enviar_mensaje(sender, text)
