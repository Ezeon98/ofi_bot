"""MercadoPago subscription API — CardForm tokenisation + webhook.

Provides:
- GET  /api/subscriptions/config      - MP public key for frontend SDK
- GET  /api/subscriptions/plans       - list available plans
- POST /api/subscriptions/create      - create preapproval with card token
- GET  /api/subscriptions/{id}/status - check subscription status
- POST /mp_webhook                    - receive MP webhook notifications
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.utils.timezone import now_ar, TZ_AR

from src.application.services.subscription_service import (
    create_subscription,
    get_available_plans,
    get_subscription_status,
    resolve_plan_type,
)
from src.infrastructure.config import get_settings
from src.infrastructure.container import UnitOfWork
from src.infrastructure.database.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter()

settings = get_settings()


def _parse_mp_datetime(value: str | None) -> datetime | None:
    """Parse an MP ISO datetime and return tz-naive Argentina dt."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(TZ_AR).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


# -- Dependencies ----------------------------------------------------


async def _get_uow(session: AsyncSession = Depends(get_session)) -> UnitOfWork:
    return UnitOfWork(session)


# -- MP public key for frontend SDK -----------------------------------


@router.get("/api/subscriptions/config")
async def get_config() -> dict[str, str]:
    return {"public_key": settings.mp_public_key}


# -- Plan listing -----------------------------------------------------


@router.get("/api/subscriptions/plans")
async def list_plans() -> list[dict[str, Any]]:
    return get_available_plans()


# -- Create subscription via card token --------------------------------


class _CreateSubscriptionBody(BaseModel):
    card_token_id: str
    payer_email: str
    plan_id: str


@router.post("/api/subscriptions/create", response_model=None)
async def create_subscription_endpoint(
    body: _CreateSubscriptionBody,
    uid: int = Query(0),
) -> dict[str, Any] | JSONResponse:
    user_id = uid if uid else None

    try:
        mp_response = await create_subscription(
            plan_id=body.plan_id,
            card_token_id=body.card_token_id,
            payer_email=body.payer_email,
            user_id=user_id,
        )
    except httpx.HTTPStatusError as exc:
        logger.error("MP preapproval error: %s %s", exc.response.status_code, exc.response.text)
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "detail": "No se pudo crear la suscripción en MercadoPago.",
            },
        )
    except ValueError as exc:
        logger.error("MP config error: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(exc)})

    return {
        "status": "ok",
        "subscription_id": mp_response.get("id", ""),
        "mp_status": mp_response.get("status", ""),
    }


# -- Subscription status ----------------------------------------------


@router.get("/api/subscriptions/{subscription_id}/status", response_model=None)
async def api_subscription_status(
    subscription_id: str,
    uow: UnitOfWork = Depends(_get_uow),
) -> dict[str, Any] | JSONResponse:
    sub = await uow.mp_subscriptions.get_by_preapproval_id(subscription_id)
    if not sub:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "detail": "Suscripcion no encontrada."},
        )

    try:
        mp_data = await get_subscription_status(subscription_id)
        live_status = mp_data.get("status", sub.status)
    except Exception:
        live_status = sub.status

    return {
        "subscription_id": subscription_id,
        "plan_type": sub.plan_type,
        "status": live_status,
        "db_status": sub.status,
        "payer_email": sub.payer_email,
        "user_id": sub.user_id,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "next_payment_date": sub.next_payment_date.isoformat() if sub.next_payment_date else None,
        "last_payment_date": sub.last_payment_date.isoformat() if sub.last_payment_date else None,
    }


# -- MercadoPago webhook ----------------------------------------------


@router.post("/mp_webhook", response_model=None)
async def mp_webhook(
    request: Request,
    uow: UnitOfWork = Depends(_get_uow),
    data_id: str = Query(None, alias="data.id"),
    event_type: str = Query(None, alias="type"),
) -> dict[str, str] | JSONResponse:
    body = await request.json()
    logger.info("MP webhook received: %s", body)

    if settings.mp_webhook_secret:
        if not _validate_webhook_signature(request, data_id):
            logger.warning("Invalid MP webhook signature")
            return JSONResponse(status_code=401, content={"status": "invalid_signature"})

    notif_type = body.get("type") or event_type or ""
    action = body.get("action", "")
    notif_data_id = body.get("data", {}).get("id") or data_id or ""

    logger.info("MP webhook: type=%s action=%s data_id=%s", notif_type, action, notif_data_id)

    try:
        if notif_type == "subscription_preapproval":
            await _handle_subscription_update(uow, notif_data_id)
        elif notif_type == "subscription_authorized_payment":
            await _handle_authorized_payment(uow, notif_data_id)
        elif notif_type == "payment":
            await _handle_payment_notification(uow, notif_data_id)
        else:
            logger.info("MP webhook unhandled type: %s", notif_type)
    except Exception:
        logger.exception("Error processing MP webhook: type=%s id=%s", notif_type, notif_data_id)

    await uow.commit()
    return {"status": "ok"}


# -- Webhook handlers -------------------------------------------------


async def _handle_subscription_update(uow: UnitOfWork, preapproval_id: str) -> None:
    """Process subscription_preapproval event."""
    if not preapproval_id:
        return

    try:
        mp_data = await get_subscription_status(preapproval_id)
    except Exception:
        logger.exception("Failed to fetch subscription %s from MP", preapproval_id)
        return

    mp_status = mp_data.get("status", "")
    payer_email = mp_data.get("payer_email", "")
    payer_id = str(mp_data.get("payer_id", ""))
    plan_id = mp_data.get("preapproval_plan_id", "")
    ext_ref = mp_data.get("external_reference", "")
    next_pay = _parse_mp_datetime(mp_data.get("next_payment_date"))

    user_id: int | None = None
    if ext_ref and ext_ref.isdigit():
        user_id = int(ext_ref)

    existing = await uow.mp_subscriptions.get_by_preapproval_id(preapproval_id)

    if existing:
        await uow.mp_subscriptions.update_status(preapproval_id, mp_status)
        if user_id and not existing.user_id:
            await uow.mp_subscriptions.link_user(preapproval_id, user_id)
        if next_pay:
            await uow.mp_subscriptions.update_payment_dates(
                preapproval_id, next_payment_date=next_pay
            )
        logger.info("Subscription %s updated: status=%s", preapproval_id, mp_status)
    else:
        plan_type = resolve_plan_type(
            plan_id,
            reason=mp_data.get("reason", ""),
            auto_recurring=mp_data.get("auto_recurring"),
        )
        await uow.mp_subscriptions.create(
            mp_preapproval_id=preapproval_id,
            plan_id=plan_id,
            plan_type=plan_type,
            payer_email=payer_email,
            status=mp_status,
            mp_payer_id=payer_id or None,
            user_id=user_id,
            next_payment_date=next_pay,
            external_reference=ext_ref or None,
        )
        logger.info("New subscription %s stored: user=%s", preapproval_id, user_id)

    # Auto-activate when subscription is authorized
    if mp_status == "authorized" and user_id:
        await _activate_user_subscription(uow, user_id, mp_data, payer_id, preapproval_id)


async def _activate_user_subscription(
    uow: UnitOfWork,
    user_id: int,
    mp_data: dict[str, Any],
    mp_payer_id: str,
    mp_subscription_id: str,
) -> None:
    """Upgrade a user's tier upon subscription authorization."""
    from sqlalchemy import select
    from src.infrastructure.database.models import UsuarioModel

    result = await uow._session.execute(select(UsuarioModel).where(UsuarioModel.id == user_id))
    usuario = result.scalar_one_or_none()
    if not usuario:
        logger.warning("Cannot activate subscription: user %s not found", user_id)
        return

    plan_type = resolve_plan_type(
        mp_data.get("preapproval_plan_id", ""),
        reason=mp_data.get("reason", ""),
        auto_recurring=mp_data.get("auto_recurring"),
    )

    if "premium" in plan_type:
        db_tier = "premium"
        sub_type = "annual_mp" if "annual" in plan_type else "monthly_mp"
    else:
        db_tier = "pro"
        sub_type = "annual_mp" if "annual" in plan_type else "monthly_mp"

    await uow.usuarios.activate_mp_subscription(
        telefono=usuario.telefono,
        subscription_type=sub_type,
        mp_payer_id=mp_payer_id,
        mp_subscription_id=mp_subscription_id,
        tier=db_tier,
    )

    from src.infrastructure.external.whatsapp_client import enviar_mensaje

    label = "Anual" if "annual" in sub_type else "Mensual"
    try:
        await enviar_mensaje(
            usuario.telefono,
            f"🎉 *¡Suscripción activada!*\n\n"
            f"Tu cuenta fue actualizada a *{db_tier.title()} {label}*.\n"
            "¡Disfrutá de todas las funciones! 🚀",
        )
    except Exception:
        logger.exception("Failed to notify user %s of activation", usuario.telefono)

    logger.info("User %s (%s) activated as %s %s", user_id, usuario.telefono, db_tier, sub_type)


async def _handle_authorized_payment(uow: UnitOfWork, payment_id: str) -> None:
    """Process a subscription_authorized_payment event."""
    if not payment_id:
        return

    headers = {"Authorization": f"Bearer {settings.mp_access_token}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.mercadopago.com/authorized_payments/{payment_id}",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("Failed to fetch authorized_payment %s", payment_id)
        return

    preapproval_id = data.get("preapproval_id", "")
    if not preapproval_id:
        return

    sub = await uow.mp_subscriptions.get_by_preapproval_id(preapproval_id)
    if not sub:
        return

    payment_date = _parse_mp_datetime(data.get("date_created")) or now_ar()
    await uow.mp_subscriptions.update_payment_dates(preapproval_id, last_payment_date=payment_date)

    logger.info("Authorized payment %s: preapproval=%s", payment_id, preapproval_id)


async def _handle_payment_notification(uow: UnitOfWork, payment_id: str) -> None:
    """Process a payment event notification."""
    if not payment_id:
        return

    headers = {"Authorization": f"Bearer {settings.mp_access_token}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("Failed to fetch payment %s from MP", payment_id)
        return

    preapproval_id = data.get("metadata", {}).get("preapproval_id", "")
    if not preapproval_id:
        return

    sub = await uow.mp_subscriptions.get_by_preapproval_id(preapproval_id)
    if not sub:
        return

    payment_date = _parse_mp_datetime(data.get("date_approved") or data.get("date_created", ""))
    if payment_date:
        await uow.mp_subscriptions.update_payment_dates(
            preapproval_id, last_payment_date=payment_date
        )


# -- Webhook signature validation -------------------------------------


def _validate_webhook_signature(request: Request, data_id: str | None) -> bool:
    x_signature = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")

    if not x_signature:
        return False

    ts = ""
    received_hash = ""
    for part in x_signature.split(","):
        key_val = part.strip().split("=", 1)
        if len(key_val) == 2:
            k, v = key_val[0].strip(), key_val[1].strip()
            if k == "ts":
                ts = v
            elif k == "v1":
                received_hash = v

    if not ts or not received_hash:
        return False

    manifest = f"id:{data_id or ''};request-id:{x_request_id};ts:{ts};"

    expected_hash = hmac.new(
        settings.mp_webhook_secret.encode(),
        manifest.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_hash, received_hash)
