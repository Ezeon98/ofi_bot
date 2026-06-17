"""Bot router — dispatches messages to the AI orchestrator."""

from __future__ import annotations

import logging

from src.infrastructure.config import get_settings
from src.infrastructure.container import UnitOfWork
from src.infrastructure.external.whatsapp_client import enviar_mensaje, enviar_typing
from src.orchestrator.ai_orchestrator import AIOrchestrator
from src.presentation.bot.handlers.menu import enviar_menu_principal

logger = logging.getLogger(__name__)

# Singleton orchestrator — built once, reused per message.
# ponytail: module-level singleton; fine for single-process deployments.
#           For multi-worker setups pass via DI container instead.
_orchestrator: AIOrchestrator | None = None


def _get_orchestrator() -> AIOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AIOrchestrator(get_settings())
    return _orchestrator


async def procesar_texto(uow: UnitOfWork, sender: str, texto: str, message_id: str = "") -> None:
    """Process an incoming text message via the AI orchestrator."""
    texto_lower = texto.strip().lower()

    # ── Hard-coded shortcuts bypass the LLM for speed / cost ─────────────
    if texto_lower in {"menu", "menú", "inicio"}:
        await enviar_menu_principal(uow, sender)
        return

    # ── AI layer ──────────────────────────────────────────────────────────
    if not get_settings().ai_enabled:
        await enviar_mensaje(sender, f"Recibido: {texto}")
        return

    orchestrator = _get_orchestrator()
    await enviar_typing(sender, message_id)
    response = await orchestrator.process(
        user_id=sender,
        message=texto,
        db=uow._session,
    )
    await enviar_mensaje(sender, response.message)
