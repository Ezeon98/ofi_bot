"""Admin panel API endpoints (login, user list, conversation log, direct messages)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as _secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.session import get_session
from src.infrastructure.external.whatsapp_client import enviar_mensaje
from src.memory.repository import ConversationRepository
from src.infrastructure.database.repositories.usuario import UsuarioRepository

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Auth ──────────────────────────────────────────────────────────────────
_admin_tokens: set[str] = set()


def _get_admin_credentials() -> tuple[str, str]:
    """Return (username, password_hash) from environment config."""
    from src.infrastructure.config import get_settings

    s = get_settings()
    return s.admin_username, s.admin_password_hash


class AdminLoginBody(BaseModel):
    """Admin login request body."""

    username: str
    password: str


def verify_admin(request: Request) -> bool:
    """Check the Authorization header for a valid admin token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] in _admin_tokens
    return False


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/api/admin/login")
async def admin_login(body: AdminLoginBody) -> dict:
    """Authenticate admin and return a session token."""
    admin_user, admin_pass_hash = _get_admin_credentials()
    if not admin_user or not admin_pass_hash:
        return JSONResponse(
            status_code=503,
            content={"error": "Admin no configurado"},
        )
    pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if body.username != admin_user or not hmac.compare_digest(pw_hash, admin_pass_hash):
        return JSONResponse(
            status_code=401,
            content={"error": "Credenciales inválidas"},
        )
    token = _secrets.token_hex(32)
    _admin_tokens.add(token)
    return {"token": token}


@router.get("/api/admin/users")
async def admin_get_users(
    request: Request,
    session: AsyncSession = Depends(get_session),
    limit: int = 20,
    offset: int = 0,
    q: str = "",
) -> JSONResponse:
    """Return paginated users with optional search and total count."""
    if not verify_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    usuarios = UsuarioRepository(session)
    total = await usuarios.count_all()
    if q.strip():
        users = await usuarios.search_users(q, limit=limit)
    else:
        users = await usuarios.get_recent_users(limit=limit, offset=offset)

    result = [
        {
            "id": u.id,
            "telefono": u.telefono,
            "nombre": u.nombre,
            "tier": usuarios.effective_tier(u),
            "fecha_registro": (u.fecha_registro.isoformat() if u.fecha_registro else None),
            "last_interaction": (
                u.last_interaction.isoformat() if u.last_interaction else None
            ),
            "accepted_terms_at": (
                u.accepted_terms_at.isoformat() if u.accepted_terms_at else None
            ),
        }
        for u in users
    ]
    return JSONResponse(content={"total": total, "users": result})


@router.get("/api/admin/users/{user_id}")
async def admin_get_user_detail(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return detailed info for a single user."""
    if not verify_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    usuarios = UsuarioRepository(session)
    usuario = await usuarios.get_user_by_id(user_id)
    if not usuario:
        return JSONResponse(status_code=404, content={"error": "Usuario no encontrado"})

    return JSONResponse(
        content={
            "id": usuario.id,
            "telefono": usuario.telefono,
            "nombre": usuario.nombre,
            "tier": usuarios.effective_tier(usuario),
            "subscription_type": usuario.subscription_type,
            "tier_expires_at": (
                usuario.tier_expires_at.isoformat() if usuario.tier_expires_at else None
            ),
            "fecha_registro": (
                usuario.fecha_registro.isoformat() if usuario.fecha_registro else None
            ),
            "last_interaction": (
                usuario.last_interaction.isoformat() if usuario.last_interaction else None
            ),
            "accepted_terms_at": (
                usuario.accepted_terms_at.isoformat() if usuario.accepted_terms_at else None
            ),
        }
    )


@router.get("/api/admin/users/{user_id}/messages")
async def admin_get_user_messages(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    limit: int = 200,
) -> JSONResponse:
    """Return the recent conversation turns (sent + received) for a user."""
    if not verify_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    usuarios = UsuarioRepository(session)
    usuario = await usuarios.get_user_by_id(user_id)
    if not usuario:
        return JSONResponse(status_code=404, content={"error": "Usuario no encontrado"})

    conversations = ConversationRepository(session)
    turns = await conversations.get_recent_turns_by_user(user_id, limit=limit)
    return JSONResponse(
        content={
            "messages": [
                {
                    "id": t.id,
                    "role": t.role,
                    "contenido": t.content,
                    "intent": t.intent,
                    "fecha": t.created_at.isoformat() if t.created_at else None,
                }
                for t in turns
            ]
        }
    )


class DirectMessageRequest(BaseModel):
    """Request body for sending a direct message to a single user."""

    message: str


class DirectMessageResponse(BaseModel):
    """Response for the direct message endpoint."""

    status: str
    telefono: str
    message_preview: str


@router.post(
    "/api/admin/users/{user_id}/send-message",
    response_model=DirectMessageResponse,
    tags=["admin"],
    summary="Send a direct WhatsApp message to a specific user",
)
async def admin_send_direct_message(
    user_id: int,
    request: Request,
    body: DirectMessageRequest,
    session: AsyncSession = Depends(get_session),
):
    """Send a WhatsApp message to a single user by their ID."""
    if not verify_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if not body.message or not body.message.strip():
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "message es requerido"},
        )

    usuarios = UsuarioRepository(session)
    usuario = await usuarios.get_user_by_id(user_id)
    if not usuario:
        return JSONResponse(status_code=404, content={"status": "error", "detail": "Usuario no encontrado"})

    try:
        await enviar_mensaje(usuario.telefono, body.message.strip())
    except Exception as exc:
        logger.error("Direct message FAIL → %s: %s", usuario.telefono, exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "detail": f"Error al enviar mensaje: {exc}"},
        )

    conversations = ConversationRepository(session)
    conversation = await conversations.get_or_create_active(user_id)
    await conversations.add_turn(conversation.id, role="assistant", content=body.message.strip())
    await session.commit()

    logger.info("Admin direct message OK → %s (user %d)", usuario.telefono, user_id)
    return DirectMessageResponse(
        status="sent",
        telefono=usuario.telefono,
        message_preview=body.message.strip()[:200],
    )
