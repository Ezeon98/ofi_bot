"""Unit of Work — aggregates all repositories for a single DB session."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.repositories import (
    EstadoRepository,
    MercadoPagoSubscriptionRepository,
    UsuarioRepository,
)


class UnitOfWork:
    """Holds all repositories and the underlying async session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.estados = EstadoRepository(session)
        self.mp_subscriptions = MercadoPagoSubscriptionRepository(session)
        self.usuarios = UsuarioRepository(session)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
