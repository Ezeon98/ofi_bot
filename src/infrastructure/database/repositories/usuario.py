"""SQLAlchemy implementation for Usuario repository."""

from datetime import datetime, timedelta

from sqlalchemy import select, insert, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.models import MessageCountModel, UsuarioModel
from src.utils.timezone import now_ar


def _now() -> datetime:
    """Current Argentina time, tz-naive for DB storage."""
    return now_ar()


class UsuarioRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, telefono: str):
        result = await self._s.execute(
            select(UsuarioModel).where(UsuarioModel.telefono == telefono)
        )
        return result.scalar_one_or_none()

    async def get_id_by_telefono(self, telefono: str) -> int | None:
        """Return the integer PK for a phone number, or None if not found."""
        result = await self._s.execute(
            select(UsuarioModel.id).where(UsuarioModel.telefono == telefono)
        )
        return result.scalar_one_or_none()

    async def get_by_bsuid(self, bsuid: str):
        """Look up a user by their Business-Scoped User ID."""
        result = await self._s.execute(
            select(UsuarioModel).where(UsuarioModel.bsuid == bsuid)
        )
        return result.scalar_one_or_none()

    async def resolve_sender(self, telefono: str, bsuid: str | None = None):
        """Resolve a sender to their phone number, linking BSUID when present."""
        usuario = await self.get(telefono)
        if usuario and bsuid and not getattr(usuario, "bsuid", None):
            await self._s.execute(
                update(UsuarioModel)
                .where(UsuarioModel.id == usuario.id)
                .values(bsuid=bsuid)
            )
        if not usuario and bsuid:
            usuario = await self.get_by_bsuid(bsuid)
            if usuario:
                telefono = usuario.telefono
        return telefono, usuario

    async def create(self, telefono: str, bsuid: str | None = None):
        """Create a new user on the free tier."""
        values = dict(telefono=telefono, tier="free")
        if bsuid:
            values["bsuid"] = bsuid
        stmt = insert(UsuarioModel).values(**values)
        await self._s.execute(stmt)
        await self._s.flush()
        await self._s.commit()

    async def update_tier(self, telefono: str, tier: str) -> bool:
        result = await self._s.execute(
            update(UsuarioModel).where(UsuarioModel.telefono == telefono).values(tier=tier)
        )
        return result.rowcount > 0

    async def activate_subscription(self, telefono: str, subscription_type: str) -> bool:
        """Activate a subscription: set tier and expiry based on type."""
        months = 12 if "annual" in subscription_type else 1
        expires_at = _now() + timedelta(days=30 * months)
        tier = "premium" if "premium" in subscription_type else "pro"
        result = await self._s.execute(
            update(UsuarioModel)
            .where(UsuarioModel.telefono == telefono)
            .values(
                tier=tier,
                subscription_type=subscription_type,
                tier_expires_at=expires_at,
            )
        )
        await self._s.commit()
        return result.rowcount > 0

    async def activate_mp_subscription(
        self,
        telefono: str,
        subscription_type: str,
        mp_payer_id: str,
        mp_subscription_id: str,
        tier: str = "pro",
    ) -> bool:
        """Activate a MercadoPago subscription with payer details."""
        months = 12 if "annual" in subscription_type else 1
        expires_at = _now() + timedelta(days=30 * months)
        result = await self._s.execute(
            update(UsuarioModel)
            .where(UsuarioModel.telefono == telefono)
            .values(
                tier=tier,
                subscription_type=subscription_type,
                tier_expires_at=expires_at,
                mp_payer_id=mp_payer_id,
                mp_subscription_id=mp_subscription_id,
                mp_subscribed_at=_now(),
            )
        )
        await self._s.commit()
        return result.rowcount > 0

    def effective_tier(self, usuario) -> str:
        """Return 'premium', 'pro', or 'free' based on subscription."""
        if not usuario:
            return "free"
        tier = getattr(usuario, "tier", "free") or "free"
        if tier not in ("pro", "premium"):
            return "free"
        expires = getattr(usuario, "tier_expires_at", None)
        if expires and expires < _now():
            return "free"
        return tier

    async def increment_message_count(self, telefono: str) -> int:
        """Increment and return today's message count for a user."""
        user = await self.get(telefono)
        if not user:
            return 0
        hoy = _now().strftime("%Y-%m-%d")
        stmt = (
            pg_insert(MessageCountModel)
            .values(usuario_id=user.id, fecha=hoy, count=1)
            .on_conflict_do_update(
                index_elements=["usuario_id", "fecha"],
                set_={"count": MessageCountModel.count + 1},
            )
            .returning(MessageCountModel.count)
        )
        result = await self._s.execute(stmt)
        row = result.scalar_one_or_none()
        return row or 1

    async def touch_interaction(self, telefono: str) -> None:
        """Record that the user just sent a message (for 24h window)."""
        await self._s.execute(
            update(UsuarioModel)
            .where(UsuarioModel.telefono == telefono)
            .values(last_interaction=_now())
        )

    async def mark_terms_accepted(self, telefono: str) -> None:
        """Persist the user's acceptance of terms and conditions."""
        await self._s.execute(
            update(UsuarioModel)
            .where(UsuarioModel.telefono == telefono)
            .values(accepted_terms_at=_now())
        )
