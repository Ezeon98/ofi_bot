"""Provider profile shortcuts for the sticky provider-profile mode.

This service handles the two explicit profile mutations currently in scope:
adding provider trades and updating the provider location.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select, update

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse, Intent
from src.application.services.provider_registration_service import (
    ProviderRegistrationService,
)
from src.application.services.provider_search_service import ProviderSearchService
from src.infrastructure.database.models import ProviderModel, UsuarioModel

logger = logging.getLogger(__name__)

ADD_TRADES_PREFIXES = (
    "agrega ",
    "agrega el rubro ",
    "agregame ",
    "suma ",
    "sumame ",
    "tambien hago ",
    "también hago ",
    "tambien trabajo de ",
    "también trabajo de ",
    "ahora tambien hago ",
    "ahora también hago ",
    "ofrezco ",
    "hago ",
    "trabajo de ",
)
PROFILE_LOCATION_HINTS = (
    "cambiar mi ubicacion",
    "cambiar mi ubicación",
    "actualizar mi ubicacion",
    "actualizar mi ubicación",
    "cambiar mi zona",
    "actualizar mi zona",
    "cambiar mi barrio",
    "actualizar mi barrio",
    "cambiar mi ciudad",
    "actualizar mi ciudad",
)


class ProviderProfileService:
    """Handle narrow provider-profile updates without the LLM."""

    def __init__(self, *, agent_logger: Any | None = None) -> None:
        """Store the optional logger used for structured turn logs."""
        self._alog = agent_logger

    async def maybe_handle_profile_update(
        self,
        *,
        user_id: str,
        message: str,
        metadata: dict[str, Any] | None,
        deps: AgentDependencies,
        turn_id: str = "",
    ) -> AgentResponse | None:
        """Handle provider profile updates that stay within the current scope."""
        location = await self._extract_location_update(message, metadata)
        if location is not None:
            return await self._handle_location_update(
                user_id=user_id,
                deps=deps,
                location=location,
                turn_id=turn_id,
            )

        rubros = self._extract_trade_update(message)
        if rubros is not None:
            if not rubros:
                return AgentResponse(
                    intent=Intent.ACTUALIZAR_PERFIL,
                    message=("Decime qué rubro querés sumar a tu perfil de prestador."),
                    confidence=1.0,
                    requires_action=False,
                )
            return await self._handle_add_trades(
                user_id=user_id,
                deps=deps,
                rubros=rubros,
                turn_id=turn_id,
            )

        if self._looks_like_location_request(message):
            return AgentResponse(
                intent=Intent.ACTUALIZAR_PERFIL,
                message=(
                    "Pasame tu nueva zona o compartime tu ubicación para "
                    "actualizar tu perfil de prestador."
                ),
                confidence=1.0,
                requires_action=False,
            )

        return None

    async def _handle_add_trades(
        self,
        *,
        user_id: str,
        deps: AgentDependencies,
        rubros: list[str],
        turn_id: str,
    ) -> AgentResponse:
        """Merge new provider trades into the existing profile."""
        provider = await self._load_provider(user_id, deps.db)
        if provider is None:
            return AgentResponse(
                intent=Intent.ACTUALIZAR_PERFIL,
                message=(
                    "Todavía no tenés un perfil de prestador creado. "
                    "Si querés, te ayudo a registrarlo primero."
                ),
                confidence=1.0,
                requires_action=False,
            )

        current_rubros = self._parse_rubros_json(provider.rubros)
        merged_rubros = list(current_rubros)
        added_rubros: list[str] = []
        current_index = {item.lower(): item for item in current_rubros}
        for rubro in rubros:
            if rubro.lower() in current_index:
                continue
            current_index[rubro.lower()] = rubro
            merged_rubros.append(rubro)
            added_rubros.append(rubro)

        self._log(
            turn_id,
            "provider_profile.add_rubros",
            requested_rubros=rubros,
            added_rubros=added_rubros,
            total_rubros=len(merged_rubros),
        )

        if not added_rubros:
            return AgentResponse(
                intent=Intent.ACTUALIZAR_PERFIL,
                message="Esos rubros ya estaban cargados en tu perfil.",
                confidence=1.0,
                entities={"rubros": current_rubros},
                requires_action=False,
            )

        await deps.db.execute(
            update(ProviderModel)
            .where(ProviderModel.id == provider.id)
            .values(rubros=json.dumps(merged_rubros, ensure_ascii=False))
        )

        return AgentResponse(
            intent=Intent.ACTUALIZAR_PERFIL,
            message=("Listo, sumé " f"{', '.join(added_rubros)} a tu perfil de prestador."),
            confidence=1.0,
            entities={"rubros": merged_rubros, "rubros_agregados": added_rubros},
            requires_action=False,
        )

    async def _handle_location_update(
        self,
        *,
        user_id: str,
        deps: AgentDependencies,
        location: dict[str, Any],
        turn_id: str,
    ) -> AgentResponse:
        """Persist a provider location update, including coordinates."""
        provider = await self._load_provider(user_id, deps.db)
        if provider is None:
            return AgentResponse(
                intent=Intent.ACTUALIZAR_PERFIL,
                message=(
                    "Todavía no tenés un perfil de prestador creado. "
                    "Si querés, te ayudo a registrarlo primero."
                ),
                confidence=1.0,
                requires_action=False,
            )

        await deps.db.execute(
            update(ProviderModel)
            .where(ProviderModel.id == provider.id)
            .values(
                barrio=location.get("barrio"),
                ciudad=location.get("ciudad"),
                lat=location.get("lat"),
                lon=location.get("lon"),
            )
        )

        self._log(
            turn_id,
            "provider_profile.update_location",
            barrio=location.get("barrio"),
            ciudad=location.get("ciudad"),
            lat=location.get("lat"),
            lon=location.get("lon"),
        )

        zone = self._location_label(location) or "tu nueva zona"
        return AgentResponse(
            intent=Intent.ACTUALIZAR_PERFIL,
            message=f"Listo, actualicé la ubicación de tu perfil a {zone}.",
            confidence=1.0,
            entities={
                "barrio": location.get("barrio"),
                "ciudad": location.get("ciudad"),
            },
            requires_action=False,
        )

    async def _extract_location_update(
        self,
        message: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Resolve an explicit provider-location update from text or metadata."""
        current_location = ProviderRegistrationService._location_from_metadata(metadata)
        if current_location is not None:
            return current_location

        zone = ProviderSearchService._extract_location_update(message)
        if zone is None:
            return None

        geocoded = await ProviderSearchService._enrich_location_with_coords({"barrio": zone})
        if geocoded.get("barrio") or geocoded.get("ciudad"):
            return geocoded
        return {"barrio": zone, "ciudad": None, "lat": None, "lon": None}

    def _extract_trade_update(self, message: str) -> list[str] | None:
        """Return parsed rubros only for explicit trade-update messages."""
        normalized = ProviderRegistrationService._normalize_message(message)
        explicit_prefix = None
        for prefix in ADD_TRADES_PREFIXES:
            if normalized.startswith(prefix):
                explicit_prefix = prefix
                break

        if explicit_prefix is None:
            if "agregar rubro" in normalized or "agregar rubros" in normalized:
                return []
            return None

        raw_message = message[len(explicit_prefix) :].strip()
        if not raw_message:
            return []
        return ProviderRegistrationService._fallback_trade_labels(raw_message)

    @staticmethod
    def _looks_like_location_request(message: str) -> bool:
        """Return True when the user asks to change location without the zone."""
        normalized = ProviderRegistrationService._normalize_message(message)
        return any(hint in normalized for hint in PROFILE_LOCATION_HINTS)

    @staticmethod
    async def _load_provider(user_id: str, db: Any) -> ProviderModel | None:
        """Load the provider row linked to the current phone number."""
        usuario_id = await db.scalar(
            select(UsuarioModel.id).where(UsuarioModel.telefono == user_id)
        )
        if usuario_id is None and user_id.isdigit():
            usuario_id = int(user_id)
        if usuario_id is None:
            return None
        return await db.scalar(select(ProviderModel).where(ProviderModel.usuario_id == usuario_id))

    @staticmethod
    def _parse_rubros_json(rubros_json: str | None) -> list[str]:
        """Parse the stored rubros JSON array, tolerating legacy invalid data."""
        if not rubros_json:
            return []
        try:
            parsed = json.loads(rubros_json)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if isinstance(item, str) and item]

    @staticmethod
    def _location_label(location: dict[str, Any]) -> str | None:
        """Render the best human-readable location label for responses."""
        parts = [location.get("barrio"), location.get("ciudad")]
        labels = [part for part in parts if isinstance(part, str) and part]
        return ", ".join(labels) if labels else None

    def _log(self, turn_id: str, event: str, **data: Any) -> None:
        """Emit structured logs when an agent logger is available."""
        if self._alog is not None and turn_id:
            self._alog.info(turn_id, event, **data)
            return
        logger.info("%s %s", event, data)
