"""Provider registration service.

Handles the guided onboarding flow for users who want to register as
service providers without relying on the LLM for every turn.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse, Intent
from src.infrastructure.database.models import ProviderModel, UsuarioModel
from src.infrastructure.database.repositories.estado import EstadoRepository
from src.memory.service import MemoryService
from src.tools.business.providers import CrearPrestadorInput, crear_prestador
from src.utils.geocoding import geocode_text_location


logger = logging.getLogger(__name__)

REGISTRATION_STATE_NAME = "provider_registration"
OFFER_SERVICES_BUTTON_ID = "post_terms_offer_services"
REGISTRATION_START_PATTERNS = (
    "quiero registrarme como proveedor",
    "quiero registrarme como prestador",
    "me quiero registrar como proveedor",
    "me quiero registrar como prestador",
    "quiero darme de alta como proveedor",
    "quiero darme de alta como prestador",
    "quiero ofrecer mis servicios",
    "quiero ofrecer servicios",
    "quiero ser proveedor",
    "quiero ser prestador",
    "quiero publicar mis servicios",
)
NAME_PREFIXES = (
    "me llamo ",
    "mi nombre es ",
    "soy ",
)
TRADE_PREFIXES = (
    "hago ",
    "me dedico a ",
    "trabajo de ",
    "ofrezco ",
    "se hacer ",
    "sé hacer ",
)
ZONE_PREFIXES = (
    "estoy en ",
    "soy de ",
    "trabajo en ",
    "mi zona es ",
    "mi barrio es ",
    "vivo en ",
)


class ProviderRegistrationService:
    """Encapsulate the guided provider-registration flow."""

    def __init__(
        self,
        *,
        memory_config: Any,
        agent_logger: Any | None = None,
    ) -> None:
        self._memory_config = memory_config
        self._alog = agent_logger

    async def maybe_handle_registration(
        self,
        *,
        user_id: str,
        message: str,
        metadata: dict[str, Any] | None,
        deps: AgentDependencies,
        memory_service: MemoryService,
        turn_id: str = "",
    ) -> AgentResponse | None:
        """Handle provider-registration turns without invoking the LLM."""
        state_repo = EstadoRepository(deps.db)
        raw_state = await state_repo.get(user_id)
        state = (
            raw_state if raw_state.get("estado") == REGISTRATION_STATE_NAME else {}
        )
        paso = state.get("paso")

        self._log(
            turn_id,
            "provider_registration.state",
            paso=paso,
            state_keys=list(state.keys()),
        )

        if not paso:
            if not self._is_registration_start(message, metadata):
                return None
            if await self._provider_exists(deps.db, user_id):
                return AgentResponse(
                    intent=Intent.REGISTRAR_PRESTADOR,
                    message=(
                        "Ya tenés un perfil de prestador creado. "
                        "Si querés, te ayudo a actualizarlo."
                    ),
                    confidence=1.0,
                    requires_action=False,
                )
            await self._save_state(state_repo, user_id, paso="awaiting_name")
            return self._build_name_request_response()

        if paso == "awaiting_name":
            nombre = self._extract_name(message)
            if nombre is None:
                return self._build_name_request_response(retry=True)
            await self._save_state(
                state_repo,
                user_id,
                paso="awaiting_age",
                nombre=nombre,
            )
            return self._build_age_request_response(nombre)

        if paso == "awaiting_age":
            edad = self._extract_age(message)
            if edad is None:
                return self._build_age_request_response(
                    state.get("nombre"),
                    retry=True,
                )
            await self._save_state(
                state_repo,
                user_id,
                paso="awaiting_trades",
                nombre=state.get("nombre"),
                edad=edad,
            )
            return self._build_trades_request_response()

        if paso == "awaiting_trades":
            rubros = self._classify_trades(message)
            if not rubros:
                return self._build_trades_request_response(retry=True)
            await self._save_state(
                state_repo,
                user_id,
                paso="awaiting_zone",
                nombre=state.get("nombre"),
                edad=state.get("edad"),
                rubros=rubros,
            )
            return self._build_zone_request_response(rubros)

        if paso == "awaiting_zone":
            location = await self._resolve_location(message, metadata)
            if location is None:
                return self._build_zone_request_response(
                    state.get("rubros") or [],
                    retry=True,
                )
            return await self._complete_registration(
                state_repo=state_repo,
                user_id=user_id,
                deps=deps,
                memory_service=memory_service,
                state=state,
                location=location,
            )

        return None

    async def _complete_registration(
        self,
        *,
        state_repo: EstadoRepository,
        user_id: str,
        deps: AgentDependencies,
        memory_service: MemoryService,
        state: dict[str, Any],
        location: dict[str, Any],
    ) -> AgentResponse:
        """Persist the provider profile after all onboarding fields are present."""
        rubros = [str(item) for item in state.get("rubros") or [] if item]
        params = CrearPrestadorInput(
            nombre=str(state.get("nombre") or "Prestador"),
            rubros=rubros or ["Servicios generales"],
            barrio=location.get("barrio"),
            ciudad=location.get("ciudad"),
        )
        result = await crear_prestador(SimpleNamespace(deps=deps), params)
        await state_repo.delete(user_id)

        entities = {
            "nombre": state.get("nombre"),
            "edad": state.get("edad"),
            "rubros": rubros,
            "barrio": location.get("barrio"),
            "ciudad": location.get("ciudad"),
        }
        if "error" in result:
            return AgentResponse(
                intent=Intent.REGISTRAR_PRESTADOR,
                message=(
                    "Ya tenés un perfil de prestador cargado. "
                    "Si querés, te ayudo a actualizarlo."
                ),
                confidence=1.0,
                entities=entities,
                requires_action=False,
            )

        if self._memory_config.enabled and state.get("edad") is not None:
            await memory_service.upsert_memory(
                deps.usuario_id,
                "provider_registration_age",
                str(state["edad"]),
                0.8,
            )

        zona = self._location_label(location) or "tu zona"
        rubros_str = ", ".join(rubros)
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=(
                f"Listo, ya inicié tu registro como prestador con {rubros_str} "
                f"en {zona}. Ahora queda pendiente de revisión para activarlo."
            ),
            confidence=1.0,
            entities=entities,
            requires_action=True,
            metadata={
                "provider_registration": {
                    "provider_id": result.get("id"),
                    "estado": result.get("estado"),
                    "edad": state.get("edad"),
                }
            },
        )

    def _classify_trades(self, message: str) -> list[str]:
        """Extract provider rubros directly from the user's free-text reply."""
        raw_message = self._strip_prefixes(message, TRADE_PREFIXES)
        if not raw_message.strip():
            return []
        return self._fallback_trade_labels(raw_message)

    async def _resolve_location(
        self,
        message: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Resolve the provider zone from shared location metadata or free text."""
        current_location = self._location_from_metadata(metadata)
        if current_location is not None:
            return current_location

        zone = self._extract_zone(message)
        if zone is None:
            return None

        geocoded = await geocode_text_location(zone)
        if geocoded:
            return {
                "lat": geocoded.get("lat"),
                "lon": geocoded.get("lon"),
                "ciudad": geocoded.get("ciudad"),
                "barrio": geocoded.get("barrio") or zone,
            }
        return {
            "lat": None,
            "lon": None,
            "ciudad": None,
            "barrio": zone,
        }

    @staticmethod
    async def _provider_exists(db: Any, user_id: str) -> bool:
        """Return True when the user already has a provider profile."""
        usuario_id = await db.scalar(
            select(UsuarioModel.id).where(UsuarioModel.telefono == user_id)
        )
        if usuario_id is None and user_id.isdigit():
            usuario_id = int(user_id)
        if usuario_id is None:
            return False
        stmt = select(ProviderModel.id).where(ProviderModel.usuario_id == usuario_id)
        return await db.scalar(stmt) is not None

    @staticmethod
    async def _save_state(
        state_repo: EstadoRepository,
        user_id: str,
        *,
        paso: str,
        nombre: str | None = None,
        edad: int | None = None,
        rubros: list[str] | None = None,
    ) -> None:
        """Persist the current onboarding step for the next user turn."""
        await state_repo.save(
            user_id,
            {
                "estado": REGISTRATION_STATE_NAME,
                "paso": paso,
                "nombre": nombre,
                "edad": edad,
                "rubros": rubros,
            },
        )

    @staticmethod
    def _is_registration_start(
        message: str,
        metadata: dict[str, Any] | None,
    ) -> bool:
        """Return True when the user explicitly asks to register as provider."""
        if metadata and (
            metadata.get("selected_id") in {"register_provider", OFFER_SERVICES_BUTTON_ID}
            or metadata.get("button_id") == OFFER_SERVICES_BUTTON_ID
        ):
            return True

        normalized = ProviderRegistrationService._normalize_message(message)
        return any(pattern in normalized for pattern in REGISTRATION_START_PATTERNS)

    @staticmethod
    def _extract_name(message: str) -> str | None:
        """Extract a usable display name from a short onboarding reply."""
        text = ProviderRegistrationService._strip_prefixes(message, NAME_PREFIXES)
        text = text.strip(" .,!?:;")
        if not text or any(char.isdigit() for char in text):
            return None

        words = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ']+", text)
        if not words:
            return None

        return " ".join(word.capitalize() for word in words[:4])

    @staticmethod
    def _extract_age(message: str) -> int | None:
        """Parse the provider age from a short reply like 'tengo 32'."""
        match = re.search(r"\b(\d{1,3})\b", message)
        if match is None:
            return None

        age = int(match.group(1))
        if age < 18 or age > 99:
            return None
        return age

    @staticmethod
    def _extract_zone(message: str) -> str | None:
        """Normalize a free-text zone reply such as a barrio or localidad."""
        text = ProviderRegistrationService._strip_prefixes(message, ZONE_PREFIXES)
        text = text.strip(" .,!?:;")
        text = re.sub(
            r"\b(?:zona de|barrio de|localidad de|la zona de|el barrio de)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return None
        return cleaned.title()

    @staticmethod
    def _fallback_trade_labels(raw_message: str) -> list[str]:
        """Split a free-text services reply into up to five provider rubro labels."""
        parts = re.split(r",|/|\by\b|\be\b", raw_message, flags=re.IGNORECASE)
        labels: list[str] = []
        for part in parts:
            candidate = ProviderRegistrationService._normalize_text(part)
            if len(candidate) < 3:
                continue
            label = candidate.title()
            if label not in labels:
                labels.append(label)
            if len(labels) == 5:
                break
        return labels

    @staticmethod
    def _strip_prefixes(message: str, prefixes: tuple[str, ...]) -> str:
        """Remove a single known conversational prefix from the user reply."""
        normalized = ProviderRegistrationService._normalize_message(message)
        for prefix in prefixes:
            if normalized.startswith(prefix):
                return message[len(prefix):].strip()
        return message.strip()

    @staticmethod
    def _location_from_metadata(
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Build a reusable location dict from channel metadata."""
        if not metadata:
            return None

        latitude = metadata.get("latitude")
        longitude = metadata.get("longitude")
        ciudad = metadata.get("ciudad")
        barrio = metadata.get("barrio")
        if latitude is None and longitude is None and not ciudad and not barrio:
            return None

        return {
            "lat": float(latitude) if isinstance(latitude, (int, float)) else None,
            "lon": float(longitude) if isinstance(longitude, (int, float)) else None,
            "ciudad": ciudad if isinstance(ciudad, str) else None,
            "barrio": barrio if isinstance(barrio, str) else None,
        }

    @staticmethod
    def _location_label(location: dict[str, Any]) -> str | None:
        """Return the best human-readable label for a provider zone."""
        parts = [location.get("barrio"), location.get("ciudad")]
        labels = [part for part in parts if isinstance(part, str) and part]
        return ", ".join(labels) if labels else None

    @staticmethod
    def _normalize_message(message: str) -> str:
        """Collapse user text into a lowercase string for heuristics."""
        return re.sub(r"\s+", " ", message.strip().lower())

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize accents and whitespace for fuzzy keyword matching."""
        folded = unicodedata.normalize("NFKD", text)
        ascii_text = "".join(char for char in folded if not unicodedata.combining(char))
        lowered = ascii_text.lower().replace("-", " ")
        lowered = re.sub(r"[^a-z0-9ñ ]+", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    @staticmethod
    def _build_name_request_response(retry: bool = False) -> AgentResponse:
        """Ask for the provider name."""
        message = (
            "Para registrarte como prestador necesito tu nombre. "
            "¿Cómo te llamás?"
        )
        if retry:
            message = "Necesito tu nombre para seguir con el registro. ¿Cómo te llamás?"
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_age_request_response(
        nombre: str | None,
        retry: bool = False,
    ) -> AgentResponse:
        """Ask for the provider age."""
        intro = f"Genial, {nombre}. " if nombre else ""
        message = f"{intro}Ahora decime tu edad."
        if retry:
            message = f"{intro}Necesito tu edad en números para seguir."
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_trades_request_response(retry: bool = False) -> AgentResponse:
        """Ask for the provider services or trades."""
        message = (
            "Contame qué trabajos sabés hacer o qué servicios ofrecés, "
            "así los clasifico en rubros."
        )
        if retry:
            message = (
                "Necesito que me cuentes qué trabajos hacés u ofrecés para "
                "clasificar tus rubros."
            )
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_zone_request_response(
        rubros: list[str],
        retry: bool = False,
    ) -> AgentResponse:
        """Ask for the provider service zone."""
        rubros_text = ", ".join(rubros)
        message = (
            f"Perfecto, te ubico en {rubros_text}. Ahora decime en qué zona "
            "trabajás o compartime tu ubicación."
        )
        if retry:
            message = (
                "Necesito tu zona de trabajo para terminar el registro. "
                "Podés escribir el barrio/localidad o compartir tu ubicación."
            )
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    def _log(self, turn_id: str, event: str, **kwargs: Any) -> None:
        """Forward structured log events to the shared agent logger."""
        if self._alog is None:
            return
        try:
            self._alog.info(turn_id, event, **kwargs)
        except Exception:
            logger.debug("Agent logger failed for event %s", event, exc_info=True)