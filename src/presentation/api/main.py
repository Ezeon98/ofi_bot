"""FastAPI application — webhook entry point for WhatsApp Business Cloud API."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.config import get_settings
from src.infrastructure.container import UnitOfWork
from src.infrastructure.database.session import get_session
from src.infrastructure.queue.processor import process_webhook_entries
from src.presentation.api.subscriptions import router as subscriptions_router

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

# Dedicated handler for the "agent" logger — outputs JSON lines to a separate
# file so you can tail/filter agent decisions independently.
_agent_logger = logging.getLogger("agent")
_agent_logger.setLevel(logging.INFO if settings.agent_logging_enabled else logging.WARNING)
_agent_handler = logging.FileHandler(
    os.path.join(os.path.dirname(settings.tmp_dir), "agent.log"),
    encoding="utf-8",
)
_agent_handler.setFormatter(logging.Formatter("%(message)s"))
_agent_logger.propagate = False
_agent_logger.addHandler(_agent_handler)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    os.makedirs(settings.tmp_dir, exist_ok=True)
    from src.schedulers.scheduler import iniciar_scheduler

    scheduler = iniciar_scheduler()
    logger.info("Scheduler started via lifespan")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped via lifespan")


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

    await process_webhook_entries(body)
    return {"status": "ok"}
