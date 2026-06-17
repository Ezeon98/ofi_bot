"""Estado (conversation state) repository."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.models import EstadoUsuarioModel, UsuarioModel


def _uid_sq(telefono: str):
    """Scalar subquery: telefono -> usuario_id."""
    return select(UsuarioModel.id).where(UsuarioModel.telefono == telefono).scalar_subquery()


async def _resolve_uid(session: AsyncSession, telefono: str) -> int | None:
    result = await session.execute(
        select(UsuarioModel.id).where(UsuarioModel.telefono == telefono)
    )
    return result.scalar_one_or_none()


class EstadoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def save(self, telefono: str, estado_dict: dict[str, Any]) -> None:
        uid = await _resolve_uid(self._s, telefono)
        if uid is None:
            return
        estado = estado_dict.get("estado", "")
        datos = {k: v for k, v in estado_dict.items() if k != "estado"}
        stmt = (
            pg_insert(EstadoUsuarioModel)
            .values(
                usuario_id=uid,
                estado=estado,
                datos=json.dumps(datos, ensure_ascii=False),
            )
            .on_conflict_do_update(
                index_elements=["usuario_id"],
                set_={"estado": estado, "datos": json.dumps(datos, ensure_ascii=False)},
            )
        )
        await self._s.execute(stmt)

    async def get(self, telefono: str) -> dict[str, Any]:
        uid = _uid_sq(telefono)
        result = await self._s.execute(
            select(EstadoUsuarioModel).where(EstadoUsuarioModel.usuario_id == uid)
        )
        row = result.scalar_one_or_none()
        if not row:
            return {}
        resultado: dict[str, Any] = {"estado": row.estado}
        try:
            extras = json.loads(row.datos) if row.datos else {}
            resultado.update(extras)
        except (json.JSONDecodeError, TypeError):
            pass
        return resultado

    async def delete(self, telefono: str) -> None:
        await self._s.execute(
            delete(EstadoUsuarioModel).where(EstadoUsuarioModel.usuario_id == _uid_sq(telefono))
        )
