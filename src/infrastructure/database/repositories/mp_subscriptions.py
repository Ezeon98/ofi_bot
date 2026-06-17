"""Repository for mp_subscriptions table."""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.models import MercadoPagoSubscriptionModel
from src.utils.timezone import now_ar


def _now_ar() -> datetime:
    return now_ar()


class MercadoPagoSubscriptionRepository:
    """CRUD operations for MercadoPago subscriptions."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        mp_preapproval_id: str,
        plan_id: str,
        plan_type: str,
        payer_email: str,
        status: str = "authorized",
        mp_payer_id: str | None = None,
        user_id: int | None = None,
        next_payment_date: datetime | None = None,
        external_reference: str | None = None,
    ) -> MercadoPagoSubscriptionModel:
        model = MercadoPagoSubscriptionModel(
            mp_preapproval_id=mp_preapproval_id,
            plan_id=plan_id,
            plan_type=plan_type,
            payer_email=payer_email,
            status=status,
            mp_payer_id=mp_payer_id,
            user_id=user_id,
            next_payment_date=next_payment_date,
            external_reference=external_reference,
        )
        self._s.add(model)
        await self._s.flush()
        return model

    async def get_by_preapproval_id(
        self, mp_preapproval_id: str
    ) -> Optional[MercadoPagoSubscriptionModel]:
        result = await self._s.execute(
            select(MercadoPagoSubscriptionModel).where(
                MercadoPagoSubscriptionModel.mp_preapproval_id == mp_preapproval_id
            )
        )
        return result.scalar_one_or_none()

    async def get_active_by_user(
        self, user_id: int
    ) -> Optional[MercadoPagoSubscriptionModel]:
        result = await self._s.execute(
            select(MercadoPagoSubscriptionModel)
            .where(
                MercadoPagoSubscriptionModel.user_id == user_id,
                MercadoPagoSubscriptionModel.status == "authorized",
            )
            .order_by(MercadoPagoSubscriptionModel.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_status(self, mp_preapproval_id: str, status: str) -> bool:
        result = await self._s.execute(
            update(MercadoPagoSubscriptionModel)
            .where(MercadoPagoSubscriptionModel.mp_preapproval_id == mp_preapproval_id)
            .values(status=status, updated_at=_now_ar())
        )
        return result.rowcount > 0

    async def update_payment_dates(
        self,
        mp_preapproval_id: str,
        *,
        last_payment_date: datetime | None = None,
        next_payment_date: datetime | None = None,
    ) -> bool:
        values: dict = {"updated_at": _now_ar()}
        if last_payment_date:
            values["last_payment_date"] = last_payment_date
        if next_payment_date:
            values["next_payment_date"] = next_payment_date
        result = await self._s.execute(
            update(MercadoPagoSubscriptionModel)
            .where(MercadoPagoSubscriptionModel.mp_preapproval_id == mp_preapproval_id)
            .values(**values)
        )
        return result.rowcount > 0

    async def link_user(self, mp_preapproval_id: str, user_id: int) -> bool:
        result = await self._s.execute(
            update(MercadoPagoSubscriptionModel)
            .where(MercadoPagoSubscriptionModel.mp_preapproval_id == mp_preapproval_id)
            .values(user_id=user_id, updated_at=_now_ar())
        )
        return result.rowcount > 0
