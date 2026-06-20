"""MercadoPago subscription service."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from src.infrastructure.config import get_settings
from src.utils.timezone import TZ_AR

logger = logging.getLogger(__name__)

_MP_SEARCH_URL = "https://api.mercadopago.com/preapproval/search"


class MercadoPagoService:
    """Handles MercadoPago subscription search & verification."""

    async def search_subscriptions(
        self,
        *,
        preapproval_plan_id: str,
        status: str | None = "authorized",
        sort: str = "date_created:desc",
        limit: int = 100,
        offset: int = 0,
        payer_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query the MercadoPago preapproval search API."""
        settings = get_settings()
        if not settings.mp_access_token:
            raise ValueError("MercadoPago access token no configurado.")

        params: dict[str, Any] = {
            "preapproval_plan_id": preapproval_plan_id,
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }
        if status:
            params["status"] = status
        if payer_id:
            params["payer_id"] = payer_id

        headers = {"Authorization": f"Bearer {settings.mp_access_token}"}

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_MP_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        return data.get("results", [])

    async def find_valid_subscription(
        self,
        confirmation_time: datetime,
        max_minutes: int = 15,
        is_subscription_used: Any = None,
        plan_id: str = "",
    ) -> dict[str, Any] | None:
        """Find a valid subscription for activation."""
        if not plan_id:
            plan_id = get_settings().mp_plan_id

        if confirmation_time.tzinfo is None:
            confirm_ar = confirmation_time.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ_AR)
        else:
            confirm_ar = confirmation_time.astimezone(TZ_AR)

        earliest = confirm_ar - timedelta(minutes=max_minutes)

        results = await self.search_subscriptions(
            preapproval_plan_id=plan_id,
            status="authorized",
            sort="date_created:desc",
        )
        for sub in results:
            created_str = sub.get("date_created", "")
            if not created_str:
                continue
            try:
                created_dt = datetime.fromisoformat(created_str)
                created_ar = created_dt.astimezone(TZ_AR)
            except (ValueError, TypeError):
                continue

            if created_ar < earliest or created_ar > confirm_ar:
                continue

            sub_id = sub.get("id", "")
            if sub_id and is_subscription_used:
                if await is_subscription_used(str(sub_id)):
                    continue

            return sub
        return None

    async def find_payment_by_payer(
        self,
        payer_id: str,
        since: datetime,
        plan_id: str = "",
    ) -> dict[str, Any] | None:
        """Find an authorized subscription for a payer created after *since*."""
        if not plan_id:
            plan_id = get_settings().mp_plan_id

        if since.tzinfo is None:
            since_ar = since.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ_AR)
        else:
            since_ar = since.astimezone(TZ_AR)

        results = await self.search_subscriptions(
            preapproval_plan_id=plan_id,
            status="authorized",
            sort="date_created:desc",
            payer_id=payer_id,
        )
        for sub in results:
            created_str = sub.get("date_created", "")
            if not created_str:
                continue
            try:
                created_dt = datetime.fromisoformat(created_str)
                created_ar = created_dt.astimezone(TZ_AR)
                if created_ar >= since_ar:
                    return sub
            except (ValueError, TypeError):
                continue
        return None
