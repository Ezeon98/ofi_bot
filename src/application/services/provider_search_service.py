"""Provider search service — extracted from AIOrchestrator.

Encapsulates the full guided-search flow:
  - Detect search intent from user messages
  - Manage conversational state (awaiting_need / awaiting_zone)
  - Resolve locations (metadata, memory, geocoding)
  - Run the provider search tool
  - Format results into bot-friendly messages

The AIOrchestrator now delegates provider-related logic to this service.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse, Intent, Message, MessageAction
from src.infrastructure.external.whatsapp_client import build_whatsapp_contact_url
from src.infrastructure.database.repositories.estado import EstadoRepository
from src.memory.service import MemoryService
from src.memory.schemas import MemoryRead
from src.tools.business.providers import BuscarPrestadoresInput, buscar_prestadores
from src.tools.business.search_state import SEARCH_STATE_NAME
from src.utils.geocoding import geocode_text_location


logger = logging.getLogger(__name__)

LOCATION_MEMORY_KEYS = {
    "lat": "search_latitude",
    "lon": "search_longitude",
    "ciudad": "search_ciudad",
    "barrio": "search_barrio",
}
SEARCH_BUTTON_ID = "post_terms_seek_services"
SEARCH_PREFIXES = (
    "buscarme un ",
    "buscarme una ",
    "buscarme ",
    "buscame un ",
    "buscame una ",
    "buscame ",
    "necesito un ",
    "necesito una ",
    "necesito ",
    "busco un ",
    "busco una ",
    "busco ",
    "quiero contratar un ",
    "quiero contratar una ",
    "quiero un ",
    "quiero una ",
    "preciso un ",
    "preciso una ",
    "preciso ",
    "me hace falta un ",
    "me hace falta una ",
    "me hace falta ",
)
SEARCH_FOLLOWUP_STOPWORDS = {
    "urgente",
    "urgentemente",
    "rápido",
    "rapido",
    "hoy",
    "ahora",
    "ya",
}
LOCATION_SPLIT_PATTERN = r"\b(?: en | por | para | cerca de )\b"
ZONE_NOISE_PREFIXES = (
    "la zona de ",
    "zona de ",
    "el barrio de ",
    "barrio de ",
    "la localidad de ",
    "localidad de ",
)
ZONE_REPLY_PREFIXES = (
    "mi ubicacion es ",
    "mi ubicación es ",
    "estoy en ",
    "vivo en ",
)


class ProviderSearchService:
    """Encapsulates all provider-search logic formerly inside AIOrchestrator."""

    def __init__(
        self,
        *,
        memory_config: Any,
        agent_logger: Any | None = None,
        openai_client: Any | None = None,
    ) -> None:
        self._memory_config = memory_config
        self._alog = agent_logger
        # openai_client kept for MemorySummarizer/Extractor if needed by callers

    # ── public entry points ────────────────────────────────────────────────

    async def maybe_handle_guided_search(
        self,
        *,
        user_id: str,
        message: str,
        metadata: dict[str, Any] | None,
        deps: AgentDependencies,
        memory_service: MemoryService,
        memories: list[MemoryRead],
        turn_id: str = "",
    ) -> AgentResponse | None:
        """Handle the simple provider-search flow without invoking the agent.

        Returns an AgentResponse if the message belongs to the search flow,
        otherwise None so the caller continues with normal agent processing.
        """
        state_repo = EstadoRepository(deps.db)
        raw_state = await state_repo.get(user_id)
        state = raw_state if raw_state.get("estado") == SEARCH_STATE_NAME else {}

        current_location = self._location_from_metadata(metadata)
        stored_location = self._location_from_memories(memories)
        search_location = current_location or stored_location
        detail = state.get("detalle")

        paso = state.get("paso")
        self._log(
            turn_id, "guided_search.state",
            paso=paso,
            has_current_location=current_location is not None,
            has_stored_location=stored_location is not None,
            has_rubro=bool(state.get("rubro")),
            state_keys=list(state.keys()),
        )

        if paso == "awaiting_need":
            rubro = self._extract_search_need(message, allow_plain=True)
            if rubro is None:
                self._log(turn_id, "guided_search.awaiting_need.no_match", message_preview=message[:80])
                return None
            if search_location is None:
                self._log(turn_id, "guided_search.awaiting_need.need_zone", rubro=rubro)
                await self._save_search_state(state_repo, user_id, "awaiting_zone", rubro)
                return self._build_location_request_response(rubro)
            self._log(turn_id, "guided_search.awaiting_need.search_ready", rubro=rubro, location=self._location_label(search_location))
            return await self._build_search_results_response(
                turn_id=turn_id,
                deps=deps,
                memory_service=memory_service,
                rubro=rubro,
                location=search_location,
                detail=detail,
            )

        if paso == "awaiting_zone" and state.get("rubro"):
            rubro = str(state["rubro"])
            typed_zone = self._extract_zone_reply(message)
            if current_location is not None:
                search_location = current_location
                self._log(turn_id, "guided_search.awaiting_zone.from_metadata", rubro=rubro)
            elif typed_zone:
                search_location = {"barrio": typed_zone}
                self._log(turn_id, "guided_search.awaiting_zone.from_text", rubro=rubro, typed_zone=typed_zone)
            elif stored_location is not None:
                search_location = stored_location
                self._log(turn_id, "guided_search.awaiting_zone.from_memory", rubro=rubro)
            else:
                self._log(turn_id, "guided_search.awaiting_zone.no_zone_found", rubro=rubro)
                return None
            return await self._build_search_results_response(
                turn_id=turn_id,
                deps=deps,
                memory_service=memory_service,
                rubro=rubro,
                location=search_location,
                detail=detail,
            )

        if not self._is_search_start(message, metadata):
            return None

        rubro = self._extract_search_need(message, allow_plain=True)
        if rubro is None:
            self._log(turn_id, "guided_search.fresh.no_rubro", message_preview=message[:80])
            await self._save_search_state(state_repo, user_id, "awaiting_need")
            return AgentResponse(
                intent=Intent.BUSCAR_SERVICIO,
                message="Decime qué servicio necesitás y te busco opciones cerca.",
                confidence=1.0,
                requires_action=False,
            )

        if search_location is None:
            inline_zone = self._extract_inline_zone(message)
            if inline_zone is not None:
                search_location = {"barrio": inline_zone}
                self._log(
                    turn_id,
                    "guided_search.fresh.inline_zone",
                    rubro=rubro,
                    typed_zone=inline_zone,
                )
            else:
                self._log(turn_id, "guided_search.fresh.need_zone", rubro=rubro)
                await self._save_search_state(state_repo, user_id, "awaiting_zone", rubro)
                return self._build_location_request_response(rubro)

        self._log(turn_id, "guided_search.fresh.search_ready", rubro=rubro, location=self._location_label(search_location))
        return await self._build_search_results_response(
            turn_id=turn_id,
            deps=deps,
            memory_service=memory_service,
            rubro=rubro,
            location=search_location,
            detail=detail,
        )

    async def maybe_reformat_provider_response(
        self,
        agent_response: AgentResponse,
        rubro: str | None = None,
        providers: list[dict[str, Any]] | None = None,
    ) -> AgentResponse:
        """If the agent's metadata contains 'providers', reformat into individual messages.

        This ensures that when the LLM returns provider results (instead of the shortcut),
        each provider is still sent as a separate message with a contact button.
        """
        effective_providers = providers
        if effective_providers is None and agent_response.metadata:
            raw_providers = agent_response.metadata.get("providers")
            if isinstance(raw_providers, list):
                effective_providers = raw_providers

        if not effective_providers:
            return agent_response

        metadata = dict(agent_response.metadata or {})
        metadata["providers"] = effective_providers
        agent_response.metadata = metadata

        effective_rubro = rubro or (agent_response.entities.get("rubro") if agent_response.entities else None) or "prestadores"
        location_label = (agent_response.entities.get("barrio") if agent_response.entities else None) or \
            (agent_response.entities.get("ciudad") if agent_response.entities else None) or "tu ubicación"

        first_message, messages = ProviderSearchService._format_provider_results(
            effective_rubro, location_label, effective_providers,
        )

        # Keep the original message as the first message, add provider messages
        agent_response.messages = messages
        if first_message:
            agent_response.message = first_message

        return agent_response

    @staticmethod
    def extract_provider_results_from_run_messages(
        run_messages: list[Any],
    ) -> list[dict[str, Any]] | None:
        """Recover raw provider results from the latest tool return in a model run."""
        for message in reversed(run_messages):
            parts = getattr(message, "parts", None)
            if not parts:
                continue

            for part in reversed(parts):
                if getattr(part, "part_kind", None) != "tool-return":
                    continue
                if getattr(part, "tool_name", None) != "tool_buscar_prestadores":
                    continue

                content = getattr(part, "content", None)
                if not isinstance(content, list) or not content:
                    return None
                if not all(isinstance(item, dict) for item in content):
                    return None
                return content

        return None

    async def store_search_location_if_available(
        self,
        memory_service: MemoryService,
        user_id: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Persist the most recent shared location so future searches can reuse it."""
        if not self._memory_config.enabled:
            return
        location = self._location_from_metadata(metadata)
        if location is None:
            return
        await self._persist_search_location(memory_service, user_id, location)

    # ── search execution ───────────────────────────────────────────────────

    async def _build_search_results_response(
        self,
        *,
        turn_id: str,
        deps: AgentDependencies,
        memory_service: MemoryService,
        rubro: str,
        location: dict[str, Any],
        detail: str | None,
    ) -> AgentResponse:
        """Run provider search directly and format the reply message."""
        location = await self._enrich_location_with_coords(location)

        params = BuscarPrestadoresInput(
            rubro=rubro,
            barrio=location.get("barrio"),
            ciudad=location.get("ciudad"),
            lat=location.get("lat"),
            lon=location.get("lon"),
            limit=5,
        )
        ctx = SimpleNamespace(deps=deps)
        self._log(
            turn_id,
            "shortcut.search_run",
            rubro=rubro, barrio=params.barrio, ciudad=params.ciudad,
            lat=params.lat, lon=params.lon,
        )
        providers = await buscar_prestadores(ctx, params)
        self._log(
            turn_id,
            "shortcut.search_result",
            rubro=rubro, provider_count=len(providers),
            provider_names=[p.get("nombre") for p in providers[:5]],
        )

        await EstadoRepository(deps.db).delete(deps.user_id)
        await self._persist_search_location(memory_service, deps.user_id, location)

        if not providers:
            location_label = self._location_label(location) or "tu zona"
            return AgentResponse(
                intent=Intent.BUSCAR_SERVICIO,
                message=(
                    f"No encontré {rubro} activos cerca de {location_label}. "
                    "Si querés, probá con otra zona o con un rubro más amplio."
                ),
                confidence=1.0,
                entities={"rubro": rubro, "barrio": location.get("barrio"),
                          "ciudad": location.get("ciudad"), "detalle": detail},
                requires_action=True,
            )

        location_label = self._location_label(location) or "tu ubicación"
        first_message, messages = ProviderSearchService._format_provider_results(
            rubro, location_label, providers,
        )
        return AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message=first_message,
            messages=messages,
            confidence=1.0,
            entities={"rubro": rubro, "barrio": location.get("barrio"),
                      "ciudad": location.get("ciudad"), "detalle": detail},
            requires_action=True,
            metadata={"providers": providers},
        )

    # ── location helpers ───────────────────────────────────────────────────

    @staticmethod
    async def _enrich_location_with_coords(
        location: dict[str, Any],
    ) -> dict[str, Any]:
        """If a location has a text location but no coordinates, resolve via geocoding."""
        if location.get("lat") is not None and location.get("lon") is not None:
            return location

        location_label = ProviderSearchService._location_label(location)
        if not location_label:
            return location

        geocoded = await geocode_text_location(location_label)
        latitude = geocoded.get("lat")
        longitude = geocoded.get("lon")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            location["lat"] = float(latitude)
            location["lon"] = float(longitude)
            if not location.get("ciudad") and geocoded.get("ciudad"):
                location["ciudad"] = geocoded["ciudad"]
            if not location.get("barrio") and geocoded.get("barrio"):
                location["barrio"] = geocoded["barrio"]

        return location

    @staticmethod
    def _build_location_request_response(rubro: str) -> AgentResponse:
        """Ask for a location once the requested service is known."""
        return AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message=(
                f"Para buscar {rubro} cerca tuyo, compartime tu ubicación de WhatsApp "
                "o escribime tu barrio o localidad."
            ),
            confidence=1.0,
            entities={"rubro": rubro},
            requires_action=False,
        )

    @staticmethod
    def _location_from_metadata(
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Build a reusable search-location dict from message metadata."""
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
    def _location_from_memories(memories: list[MemoryRead]) -> dict[str, Any] | None:
        """Load the last shared location from persistent user memory."""
        values = {memory.key: memory.value for memory in memories}
        lat = ProviderSearchService._parse_float(values.get(LOCATION_MEMORY_KEYS["lat"]))
        lon = ProviderSearchService._parse_float(values.get(LOCATION_MEMORY_KEYS["lon"]))
        ciudad = values.get(LOCATION_MEMORY_KEYS["ciudad"])
        barrio = values.get(LOCATION_MEMORY_KEYS["barrio"])
        if lat is None and lon is None and not ciudad and not barrio:
            return None
        return {
            "lat": lat,
            "lon": lon,
            "ciudad": ciudad,
            "barrio": barrio,
        }

    @staticmethod
    def _location_label(location: dict[str, Any]) -> str | None:
        """Return the best human-readable label for a search location."""
        parts = [location.get("barrio"), location.get("ciudad")]
        labels = [part for part in parts if isinstance(part, str) and part]
        return ", ".join(labels) if labels else None

    @staticmethod
    def _format_provider_results(
        rubro: str,
        location_label: str,
        providers: list[dict[str, Any]],
        mensaje_contacto: str = "Hola, te contacto por ServiMatch para consultar sobre tus servicios.",
    ) -> tuple[str, list[Message]]:
        """Render the provider shortlist as individual messages with contact buttons."""
        count = len(providers)
        first_message = f"Encontré {count} {rubro} cerca de {location_label}:"

        messages: list[Message] = []
        for provider in providers:
            rubros_list = provider.get("rubros") or []
            rubros_str = ", ".join(rubros_list) if rubros_list else rubro

            lines = [f"👤 {provider['nombre']}"]
            if rubros_str:
                lines.append(f"🔧 {rubros_str}")
            if provider.get("badge_verificado"):
                lines.append("✅ Verificado")
            zone = provider.get("barrio") or provider.get("ciudad") or ""
            if zone:
                lines.append(f"📍 {zone}")
            distance = provider.get("distance_km")
            if isinstance(distance, (int, float)):
                lines.append(f"📏 {distance:.1f} km")
            text = "\n".join(lines)

            telefono = provider.get("telefono")
            action: MessageAction | None = None
            if telefono:
                wa_url = build_whatsapp_contact_url(telefono, mensaje_contacto)
                action = MessageAction(type="cta_url", label="Contactar", url=wa_url)

            messages.append(Message(text=text, action=action))

        return first_message, messages

    # ── intent extraction helpers ──────────────────────────────────────────

    @staticmethod
    def _is_search_start(
        message: str,
        metadata: dict[str, Any] | None,
    ) -> bool:
        """Return True when the message explicitly starts a service search."""
        if metadata and metadata.get("selected_id") == SEARCH_BUTTON_ID:
            return True

        normalized = ProviderSearchService._normalize_message(message)
        if normalized in {
            "quiero buscar servicios",
            "busco servicios",
            "buscar servicios",
            "contratar un servicio",
            "contratar servicio",
        }:
            return True
        if any(normalized.startswith(prefix) for prefix in SEARCH_PREFIXES):
            return True

        return (
            ProviderSearchService._extract_search_need(message, allow_plain=True) is not None
            and ProviderSearchService._extract_location_fragment(normalized) is not None
        )

    @staticmethod
    def _extract_search_need(message: str, allow_plain: bool) -> str | None:
        """Extract the requested trade from a search message or short follow-up."""
        normalized = ProviderSearchService._normalize_message(message)
        candidate = normalized
        for prefix in SEARCH_PREFIXES:
            if normalized.startswith(prefix):
                candidate = normalized[len(prefix):]
                break
        else:
            if not allow_plain:
                return None

        candidate = re.split(LOCATION_SPLIT_PATTERN, candidate, maxsplit=1)[0]
        words = [
            word
            for word in re.findall(r"[a-záéíóúñ]+", candidate)
            if word not in SEARCH_FOLLOWUP_STOPWORDS
        ]
        if not words:
            return None

        if len(words) == 1 and words[0] in {"servicio", "servicios", "ayuda"}:
            return None
        return " ".join(words[:3])

    @staticmethod
    def _extract_inline_zone(message: str) -> str | None:
        """Extract a zone written inside the initial search message."""
        normalized = ProviderSearchService._normalize_message(message)
        candidate = normalized
        for prefix in SEARCH_PREFIXES:
            if normalized.startswith(prefix):
                candidate = normalized[len(prefix) :]
                break
        else:
            if ProviderSearchService._extract_search_need(message, allow_plain=True) is None:
                return None

        parts = re.split(LOCATION_SPLIT_PATTERN, candidate, maxsplit=1)
        if len(parts) < 2:
            return None

        zone = ProviderSearchService._clean_zone_text(parts[1])
        if not zone:
            return None

        words = zone.split()
        if len(words) > 6:
            return None
        return zone

    @staticmethod
    def _extract_zone_reply(message: str) -> str | None:
        """Normalize a location follow-up such as a barrio or locality name."""
        normalized = ProviderSearchService._normalize_message(message)
        for prefix in ZONE_REPLY_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break

        if not normalized:
            return None

        if (
            ProviderSearchService._is_search_start(message, None)
            and ProviderSearchService._extract_location_fragment(normalized) is None
        ):
            return None

        zone = ProviderSearchService._extract_location_fragment(normalized) or ProviderSearchService._clean_zone_text(normalized)
        words = zone.split()
        if not words or len(words) > 6:
            return None
        return zone

    @staticmethod
    def _extract_location_fragment(text: str) -> str | None:
        """Return the location suffix after a connector like 'en' when present."""
        parts = re.split(LOCATION_SPLIT_PATTERN, text, maxsplit=1)
        if len(parts) < 2:
            return None

        zone = ProviderSearchService._clean_zone_text(parts[1])
        return zone or None

    @staticmethod
    def _clean_zone_text(text: str) -> str:
        """Remove common filler around free-text locations."""
        zone = text.strip(" .,!?:;")
        while True:
            lowered = zone.lower()
            for prefix in ZONE_NOISE_PREFIXES:
                if lowered.startswith(prefix):
                    zone = zone[len(prefix):].strip(" .,!?:;")
                    break
            else:
                return zone

    # ── state persistence ─────────────────────────────────────────────────

    @staticmethod
    async def _save_search_state(
        state_repo: EstadoRepository,
        user_id: str,
        paso: str,
        rubro: str | None = None,
        detalle: str | None = None,
    ) -> None:
        """Persist the current guided-search step for the next turn."""
        await state_repo.save(
            user_id,
            {
                "estado": SEARCH_STATE_NAME,
                "paso": paso,
                "rubro": rubro,
                "detalle": detalle,
            },
        )

    async def _persist_search_location(
        self,
        memory_service: MemoryService,
        user_id: str,
        location: dict[str, Any],
    ) -> None:
        """Upsert coarse search-location fields into persistent user memory."""
        if not self._memory_config.enabled:
            return

        lat = location.get("lat")
        lon = location.get("lon")
        ciudad = location.get("ciudad")
        barrio = location.get("barrio")

        if lat is not None and lon is not None:
            await memory_service.upsert_memory(
                user_id,
                LOCATION_MEMORY_KEYS["lat"],
                str(lat),
                0.95,
            )
            await memory_service.upsert_memory(
                user_id,
                LOCATION_MEMORY_KEYS["lon"],
                str(lon),
                0.95,
            )
        if ciudad:
            await memory_service.upsert_memory(
                user_id,
                LOCATION_MEMORY_KEYS["ciudad"],
                ciudad,
                0.8,
            )
        if barrio:
            await memory_service.upsert_memory(
                user_id,
                LOCATION_MEMORY_KEYS["barrio"],
                barrio,
                0.8,
            )

    # ── utilities ──────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_message(message: str) -> str:
        """Collapse user text into a simple lowercase string for heuristics."""
        return re.sub(r"\s+", " ", message.strip().lower())

    @staticmethod
    def _parse_float(value: str | None) -> float | None:
        """Parse a float from memory storage, returning None on invalid values."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _log(self, turn_id: str, event: str, **kwargs: Any) -> None:
        """Forward structured log events to the agent logger when available."""
        if self._alog is None:
            return
        try:
            self._alog.info(turn_id, event, **kwargs)
        except Exception:  # pragma: no cover — defensive, never fail search flow
            logger.debug("Agent logger failed for event %s", event, exc_info=True)