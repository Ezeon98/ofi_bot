"""Subscription service — MP preapproval via CardForm tokenisation."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.infrastructure.config import get_settings

logger = logging.getLogger(__name__)

_MP_PREAPPROVAL_URL = "https://api.mercadopago.com/preapproval"

_WHATSAPP_BACK_URL = "https://wa.me/YOUR_BOT_NUMBER"


def resolve_plan_type(
    plan_id: str = "",
    *,
    reason: str = "",
    auto_recurring: dict[str, Any] | None = None,
) -> str:
    """Derive a human-readable plan type from plan_id or metadata."""
    s = get_settings()
    mapping: dict[str, str] = {
        s.mp_plan_monthly_trial: "pro_monthly_trial",
        s.mp_plan_monthly_no_trial: "pro_monthly",
        s.mp_plan_annual_trial: "pro_annual_trial",
        s.mp_plan_annual_no_trial: "pro_annual",
        s.mp_plan_id: "pro_monthly_trial",
        s.mp_plan_id_no_trial: "pro_monthly",
        s.mp_premium_monthly_trial: "premium_monthly_trial",
        s.mp_premium_monthly_no_trial: "premium_monthly",
        s.mp_premium_annual_trial: "premium_annual_trial",
        s.mp_premium_annual_no_trial: "premium_annual",
    }
    if plan_id and plan_id in mapping:
        return mapping[plan_id]

    reason_lower = reason.lower()
    is_premium = "premium" in reason_lower
    is_annual = "anual" in reason_lower or "annual" in reason_lower

    if is_premium:
        return "premium_annual" if is_annual else "premium_monthly"
    if is_annual:
        return "pro_annual"

    if auto_recurring:
        freq = auto_recurring.get("frequency", 1)
        if freq >= 12:
            return "pro_annual"
        return "pro_monthly"

    return "unknown"


def get_available_plans() -> list[dict[str, Any]]:
    """Return the list of available subscription plans."""
    s = get_settings()
    plans: list[dict[str, Any]] = []

    if s.mp_plan_monthly_trial:
        plans.append({
            "id": s.mp_plan_monthly_trial,
            "type": "pro_monthly_trial",
            "tier": "pro",
            "name": "Pro Mensual",
            "price": s.pro_monthly_price,
            "currency": "ARS",
            "frequency": "monthly",
            "has_free_trial": True,
        })
    if s.mp_plan_monthly_no_trial:
        plans.append({
            "id": s.mp_plan_monthly_no_trial,
            "type": "pro_monthly",
            "tier": "pro",
            "name": "Pro Mensual",
            "price": s.pro_monthly_price,
            "currency": "ARS",
            "frequency": "monthly",
            "has_free_trial": False,
        })
    if s.mp_plan_annual_trial:
        plans.append({
            "id": s.mp_plan_annual_trial,
            "type": "pro_annual_trial",
            "tier": "pro",
            "name": "Pro Anual",
            "price": s.pro_annual_price,
            "currency": "ARS",
            "frequency": "yearly",
            "has_free_trial": True,
        })
    if s.mp_plan_annual_no_trial:
        plans.append({
            "id": s.mp_plan_annual_no_trial,
            "type": "pro_annual",
            "tier": "pro",
            "name": "Pro Anual",
            "price": s.pro_annual_price,
            "currency": "ARS",
            "frequency": "yearly",
            "has_free_trial": False,
        })

    if s.mp_premium_monthly_trial:
        plans.append({
            "id": s.mp_premium_monthly_trial,
            "type": "premium_monthly_trial",
            "tier": "premium",
            "name": "Premium Mensual",
            "price": s.premium_monthly_price,
            "currency": "ARS",
            "frequency": "monthly",
            "has_free_trial": True,
        })
    if s.mp_premium_monthly_no_trial:
        plans.append({
            "id": s.mp_premium_monthly_no_trial,
            "type": "premium_monthly",
            "tier": "premium",
            "name": "Premium Mensual",
            "price": s.premium_monthly_price,
            "currency": "ARS",
            "frequency": "monthly",
            "has_free_trial": False,
        })
    if s.mp_premium_annual_trial:
        plans.append({
            "id": s.mp_premium_annual_trial,
            "type": "premium_annual_trial",
            "tier": "premium",
            "name": "Premium Anual",
            "price": s.premium_annual_price,
            "currency": "ARS",
            "frequency": "yearly",
            "has_free_trial": True,
        })
    if s.mp_premium_annual_no_trial:
        plans.append({
            "id": s.mp_premium_annual_no_trial,
            "type": "premium_annual",
            "tier": "premium",
            "name": "Premium Anual",
            "price": s.premium_annual_price,
            "currency": "ARS",
            "frequency": "yearly",
            "has_free_trial": False,
        })

    return plans


async def create_subscription(
    *,
    plan_id: str,
    card_token_id: str,
    payer_email: str,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Create an MP preapproval linked to a plan using a card token."""
    settings = get_settings()
    if not settings.mp_access_token:
        raise ValueError("MP_ACCESS_TOKEN no configurado.")

    payload: dict[str, Any] = {
        "preapproval_plan_id": plan_id,
        "card_token_id": card_token_id,
        "payer_email": payer_email,
        "status": "authorized",
        "back_url": _WHATSAPP_BACK_URL,
    }
    if user_id is not None:
        payload["external_reference"] = str(user_id)

    headers = {
        "Authorization": f"Bearer {settings.mp_access_token}",
        "Content-Type": "application/json",
    }

    logger.info(
        "Creating MP preapproval: plan=%s user_id=%s email=%s",
        plan_id, user_id, payer_email,
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_MP_PREAPPROVAL_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    logger.info(
        "MP preapproval created: id=%s status=%s",
        data.get("id"), data.get("status"),
    )
    return data


async def cancel_subscription(preapproval_id: str) -> dict[str, Any]:
    """Cancel an MP preapproval subscription via PUT."""
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.mp_access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{_MP_PREAPPROVAL_URL}/{preapproval_id}",
            json={"status": "cancelled"},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def get_subscription_status(preapproval_id: str) -> dict[str, Any]:
    """Fetch current status of a preapproval from MP."""
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.mp_access_token}"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_MP_PREAPPROVAL_URL}/{preapproval_id}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()
