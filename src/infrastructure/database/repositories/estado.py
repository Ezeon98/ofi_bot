"""Estado (conversation state) repository."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.models import EstadoUsuarioModel, UsuarioModel

MODE_COORDINATOR_STATE_NAME = "mode_coordinator"
MODE_PROVIDER_PROFILE = "provider_profile"
MODE_PROVIDER_SEARCH = "provider_search"
MODE_TO_FLOW_STATE = {
    MODE_PROVIDER_PROFILE: "provider_registration",
    MODE_PROVIDER_SEARCH: "guided_provider_search",
}
FLOW_STATE_TO_MODE = {flow_state: mode for mode, flow_state in MODE_TO_FLOW_STATE.items()}


def _uid_sq(telefono: str):
    """Scalar subquery: telefono -> usuario_id."""
    return select(UsuarioModel.id).where(UsuarioModel.telefono == telefono).scalar_subquery()


async def _resolve_uid(session: AsyncSession, telefono: str) -> int | None:
    result = await session.execute(select(UsuarioModel.id).where(UsuarioModel.telefono == telefono))
    return result.scalar_one_or_none()


def _decode_datos(raw_datos: str | None) -> dict[str, Any]:
    """Parse the JSON payload stored in usuario_estado.datos."""
    try:
        decoded = json.loads(raw_datos) if raw_datos else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _build_mode_payload(
    estado: str,
    datos: dict[str, Any],
) -> dict[str, Any]:
    """Normalize legacy or wrapped rows into the mode-coordinator shape."""
    if estado == MODE_COORDINATOR_STATE_NAME:
        flows = datos.get("flows")
        return {
            "active_mode": datos.get("active_mode"),
            "pending_mode": datos.get("pending_mode"),
            "pending_confirmation": bool(datos.get("pending_confirmation")),
            "flows": flows if isinstance(flows, dict) else {},
        }

    inferred_mode = FLOW_STATE_TO_MODE.get(estado)
    flows = {estado: datos} if estado else {}
    return {
        "active_mode": inferred_mode,
        "pending_mode": None,
        "pending_confirmation": False,
        "flows": flows,
    }


def _active_flow_payload(mode_payload: dict[str, Any]) -> dict[str, Any]:
    """Return the active guided-flow payload for legacy callers."""
    active_mode = mode_payload.get("active_mode")
    if not isinstance(active_mode, str):
        return {}

    flow_state = MODE_TO_FLOW_STATE.get(active_mode)
    if flow_state is None:
        return {}

    flows = mode_payload.get("flows")
    if not isinstance(flows, dict):
        return {}

    active_flow = flows.get(flow_state)
    if not isinstance(active_flow, dict):
        return {}

    result: dict[str, Any] = {"estado": flow_state}
    result.update(active_flow)
    return result


class EstadoRepository:
    """Persist one per-user state row while supporting dual top-level modes."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the async session used for state reads and writes."""
        self._s = session

    async def save(self, telefono: str, estado_dict: dict[str, Any]) -> None:
        """Persist a guided-flow state without discarding wrapped mode state."""
        uid = await _resolve_uid(self._s, telefono)
        if uid is None:
            return

        estado = estado_dict.get("estado", "")
        datos = {k: v for k, v in estado_dict.items() if k != "estado"}

        current_row = await self._get_row_by_uid(uid)
        if current_row is not None and current_row.estado == MODE_COORDINATOR_STATE_NAME:
            mode_payload = _build_mode_payload(
                current_row.estado,
                _decode_datos(current_row.datos),
            )
            flows = dict(mode_payload.get("flows") or {})
            flows[estado] = datos
            mode_payload["flows"] = flows
            if not mode_payload.get("active_mode"):
                mode_payload["active_mode"] = FLOW_STATE_TO_MODE.get(estado)
            await self._save_mode_payload(uid, mode_payload)
            return

        await self._upsert_row(uid, estado, datos)

    async def get(self, telefono: str) -> dict[str, Any]:
        """Return the active guided-flow payload for legacy service callers."""
        uid = _uid_sq(telefono)
        result = await self._s.execute(
            select(EstadoUsuarioModel).where(EstadoUsuarioModel.usuario_id == uid)
        )
        row = result.scalar_one_or_none()
        if not row:
            return {}

        if row.estado == MODE_COORDINATOR_STATE_NAME:
            return _active_flow_payload(_build_mode_payload(row.estado, _decode_datos(row.datos)))

        resultado: dict[str, Any] = {"estado": row.estado}
        resultado.update(_decode_datos(row.datos))
        return resultado

    async def delete(self, telefono: str) -> None:
        """Delete only the active guided flow when a mode wrapper is present."""
        uid = await _resolve_uid(self._s, telefono)
        if uid is None:
            return

        row = await self._get_row_by_uid(uid)
        if row is None:
            return

        if row.estado == MODE_COORDINATOR_STATE_NAME:
            mode_payload = _build_mode_payload(
                row.estado,
                _decode_datos(row.datos),
            )
            active_mode = mode_payload.get("active_mode")
            flow_state = MODE_TO_FLOW_STATE.get(active_mode)
            flows = dict(mode_payload.get("flows") or {})
            if flow_state is not None:
                flows.pop(flow_state, None)
            mode_payload["flows"] = flows
            if self._has_mode_payload(mode_payload):
                await self._save_mode_payload(uid, mode_payload)
            else:
                await self._delete_row_by_uid(uid)
            return

        await self._delete_row_by_uid(uid)

    async def get_mode(self, telefono: str) -> dict[str, Any]:
        """Return the full top-level mode wrapper for orchestrator routing."""
        uid = _uid_sq(telefono)
        result = await self._s.execute(
            select(EstadoUsuarioModel).where(EstadoUsuarioModel.usuario_id == uid)
        )
        row = result.scalar_one_or_none()
        if not row:
            return {
                "active_mode": None,
                "pending_mode": None,
                "pending_confirmation": False,
                "flows": {},
            }
        return _build_mode_payload(row.estado, _decode_datos(row.datos))

    async def save_mode(
        self,
        telefono: str,
        *,
        active_mode: str | None,
        pending_mode: str | None = None,
        pending_confirmation: bool = False,
    ) -> None:
        """Persist top-level mode metadata while preserving existing flow data."""
        uid = await _resolve_uid(self._s, telefono)
        if uid is None:
            return

        row = await self._get_row_by_uid(uid)
        mode_payload = {
            "active_mode": active_mode,
            "pending_mode": pending_mode,
            "pending_confirmation": pending_confirmation,
            "flows": {},
        }
        if row is not None:
            mode_payload = _build_mode_payload(row.estado, _decode_datos(row.datos))
            mode_payload["active_mode"] = active_mode
            mode_payload["pending_mode"] = pending_mode
            mode_payload["pending_confirmation"] = pending_confirmation

        await self._save_mode_payload(uid, mode_payload)

    async def request_mode_switch(
        self,
        telefono: str,
        *,
        active_mode: str | None,
        pending_mode: str,
    ) -> None:
        """Store a pending mode switch awaiting explicit user confirmation."""
        await self.save_mode(
            telefono,
            active_mode=active_mode,
            pending_mode=pending_mode,
            pending_confirmation=True,
        )

    async def clear_pending_mode(self, telefono: str) -> None:
        """Clear any pending mode switch while keeping the active mode intact."""
        mode_payload = await self.get_mode(telefono)
        await self.save_mode(
            telefono,
            active_mode=mode_payload.get("active_mode"),
            pending_mode=None,
            pending_confirmation=False,
        )

    async def _get_row_by_uid(self, uid: int) -> EstadoUsuarioModel | None:
        """Load the raw state row by internal user id."""
        result = await self._s.execute(
            select(EstadoUsuarioModel).where(EstadoUsuarioModel.usuario_id == uid)
        )
        return result.scalar_one_or_none()

    async def _save_mode_payload(self, uid: int, mode_payload: dict[str, Any]) -> None:
        """Persist the normalized mode-coordinator payload."""
        datos = {
            "active_mode": mode_payload.get("active_mode"),
            "pending_mode": mode_payload.get("pending_mode"),
            "pending_confirmation": bool(mode_payload.get("pending_confirmation")),
            "flows": (
                mode_payload.get("flows") if isinstance(mode_payload.get("flows"), dict) else {}
            ),
        }
        await self._upsert_row(uid, MODE_COORDINATOR_STATE_NAME, datos)

    async def _upsert_row(
        self,
        uid: int,
        estado: str,
        datos: dict[str, Any],
    ) -> None:
        """Insert or replace the per-user state row."""
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

    async def _delete_row_by_uid(self, uid: int) -> None:
        """Delete the raw state row by internal user id."""
        await self._s.execute(
            delete(EstadoUsuarioModel).where(EstadoUsuarioModel.usuario_id == uid)
        )

    @staticmethod
    def _has_mode_payload(mode_payload: dict[str, Any]) -> bool:
        """Return True when the wrapper still carries meaningful state."""
        return bool(
            mode_payload.get("active_mode")
            or mode_payload.get("pending_mode")
            or mode_payload.get("pending_confirmation")
            or mode_payload.get("flows")
        )
