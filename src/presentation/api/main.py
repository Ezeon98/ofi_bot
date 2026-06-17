"""FastAPI application — webhook entry point for WhatsApp Business Cloud API."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.config import get_settings
from src.infrastructure.container import UnitOfWork
from src.infrastructure.database.session import get_session
from src.infrastructure.external.whatsapp_client import enviar_mensaje
from src.presentation.bot.router import procesar_texto
from src.presentation.bot.handlers.menu import enviar_menu_principal
from src.presentation.api.subscriptions import router as subscriptions_router
from src.utils.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _noisy in ("sqlalchemy", "httpx", "httpcore", "asyncpg", "watchfiles"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    os.makedirs(settings.tmp_dir, exist_ok=True)
    yield


app = FastAPI(title="WhatsApp Bot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount(
    "/suscripcion/assets",
    StaticFiles(directory="frontend"),
    name="frontend-assets",
)
app.include_router(subscriptions_router, tags=["subscriptions"])


# ── Serve frontend subscription page ─────────────────────────────────────────
@app.get("/suscripcion", response_model=None)
async def serve_subscription_page() -> Response:
    """Return the CardForm-based subscription frontend."""
    import pathlib

    html_path = pathlib.Path("frontend/index.html")
    if not html_path.exists():
        return JSONResponse(status_code=404, content={"detail": "Frontend not found."})
    return Response(
        content=html_path.read_text(encoding="utf-8"),
        media_type="text/html",
    )


# ── Dependency ────────────────────────────────────────────────────────────────
async def get_uow(session: AsyncSession = Depends(get_session)) -> UnitOfWork:
    return UnitOfWork(session)


# ── Webhook verification ──────────────────────────────────────────────────────
@app.get("/wp_webhook", response_model=None)
async def verify_webhook(request: Request) -> Response:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == settings.verify_token:
        logger.info("Webhook verificado correctamente")
        return Response(content=challenge, media_type="text/plain")
    return JSONResponse(status_code=403, content={"error": "Token inválido"})


# ── Incoming messages ─────────────────────────────────────────────────────────
@app.post("/wp_webhook")
async def webhook(request: Request) -> dict[str, str]:
    body = await request.json()
    if body.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    asyncio.create_task(_process_webhook_body(body))
    return {"status": "ok"}


async def _process_webhook_body(body: dict) -> None:
    """Process webhook payload in background with its own DB session."""
    from src.infrastructure.database.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            uow = UnitOfWork(session)
            await _handle_webhook_entries(uow, body)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _handle_webhook_entries(uow: UnitOfWork, body: dict) -> None:
    """Iterate webhook entries and process each message."""
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

                _HANDLED_TYPES = {"text", "interactive"}
                if msg_type not in _HANDLED_TYPES:
                    logger.debug("Tipo no manejado ignorado: %s", msg_type)
                    continue

                if is_duplicate(msg_id):
                    logger.info("Mensaje duplicado ignorado: %s", msg_id)
                    continue

                if is_old_message(msg_timestamp):
                    logger.info("Mensaje antiguo ignorado: %s", sender)
                    continue

                if not check_rate_limit(sender):
                    logger.warning("Rate limit excedido para %s", sender)
                    await enviar_mensaje(
                        sender, "⏳ Estás enviando mensajes muy rápido. Esperá un momento.",
                    )
                    continue

                try:
                    # Resolve sender (BSUID migration support)
                    sender, usuario = await uow.usuarios.resolve_sender(sender, bsuid)
                    if not usuario:
                        await uow.usuarios.create(sender, bsuid)

                    await uow.usuarios.touch_interaction(sender)

                    match msg_type:
                        case "text":
                            texto = message["text"]["body"]
                            await procesar_texto(uow, sender, texto, msg_id)
                        case "interactive":
                            interactive = message.get("interactive", {})
                            int_type = interactive.get("type")
                            if int_type == "list_reply":
                                selected_id = interactive["list_reply"]["id"]
                                # TODO: Add your selection handler here
                                await enviar_mensaje(sender, f"Seleccionaste: {selected_id}")
                            elif int_type == "button_reply":
                                btn_id = interactive["button_reply"]["id"]
                                # TODO: Add your button handler here
                                await enviar_mensaje(sender, f"Botón: {btn_id}")
                except Exception as exc:
                    logger.exception("Error procesando mensaje de %s", sender)
                    await enviar_mensaje(
                        sender,
                        "❌ Ocurrió un error inesperado. Intentá de nuevo.",
                    )
