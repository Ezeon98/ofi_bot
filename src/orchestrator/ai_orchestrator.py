"""AIOrchestrator — the single public entry point for the AI layer.

The bot (or any other channel) calls:

    response = await orchestrator.process(
        user_id=user_id,
        message=message,
        metadata=metadata,
    )

and receives a structured OrchestratorResponse. Everything else is internal.

Responsibilities (in order):
  1. Retrieve persistent memories
  2. Retrieve recent conversation history
  3. Build context (ContextBuilder)
  4. Construct AgentDependencies
  5. Run the router agent
  6. Post-process: extract facts, persist turns, prune, maybe summarise
  7. Commit DB changes
  8. Return OrchestratorResponse
"""

from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse, Intent
from src.agents.router_agent import router_agent
from src.context.builder import ContextBuilder
from src.infrastructure.config import Settings
from src.infrastructure.database.repositories.estado import EstadoRepository
from src.memory.extractor import MemoryExtractor
from src.memory.models import MemoryConfig
from src.memory.schemas import MemoryRead
from src.memory.service import MemoryService
from src.memory.summarizer import MemorySummarizer
from src.tools.business.providers import BuscarPrestadoresInput, buscar_prestadores
from src.tools.business.search_state import SEARCH_STATE_NAME
from src.utils.agent_logger import AgentLogger
from src.utils.geocoding import geocode_text_location

logger = logging.getLogger(__name__)

LOCATION_MEMORY_KEYS = {
    "lat": "search_latitude",
    "lon": "search_longitude",
    "ciudad": "search_ciudad",
    "barrio": "search_barrio",
    "zona": "search_zona",
}
SEARCH_BUTTON_ID = "post_terms_seek_services"
SEARCH_PREFIXES = (
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
ZONE_REPLY_PREFIXES = (
    "mi ubicacion es ",
    "mi ubicación es ",
    "estoy en ",
    "vivo en ",
)


class OrchestratorResponse(BaseModel):
    """What the bot layer receives — channel-agnostic."""

    message: str
    intent: str
    confidence: float
    entities: dict[str, Any] | None = None
    requires_action: bool = False
    metadata: dict[str, Any] | None = None


class AIOrchestrator:
    """Coordinates context, memory and agent for a single message turn.

    Instantiate once per application lifecycle (or per request if you prefer
    short-lived objects — the stateless agent handles both).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._memory_config = MemoryConfig(
            enabled=settings.memory_enabled,
            max_memories=settings.memory_max_memories,
            max_tokens=settings.memory_max_tokens,
            summarize_after=settings.memory_summarize_after,
            importance_threshold=settings.memory_importance_threshold,
        )
        self._openai = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
        self._context_builder = ContextBuilder(max_tokens=self._memory_config.max_tokens)
        self._alog = AgentLogger(enabled=settings.agent_logging_enabled)

    async def process(
        self,
        *,
        user_id: str,
        message: str,
        db: AsyncSession,
        metadata: dict[str, Any] | None = None,
    ) -> OrchestratorResponse:
        """Full AI pipeline for one user message."""
        turn_id = self._alog.new_turn_id()
        _start = time.monotonic()

        # ── Pipeline entry ────────────────────────────────────────────────
        self._alog.info(
            turn_id, "pipeline.start",
            user_id=user_id,
            message_length=len(message),
            message_preview=message[:120],
            metadata_keys=list(metadata.keys()) if metadata else None,
        )

        memory_service = self._build_memory_service(db)
        await self._store_search_location_if_available(
            memory_service,
            user_id,
            metadata,
        )

        # ── 1-3. Memory + history + context ──────────────────────────────
        memories = await memory_service.get_memories(user_id)
        conversation = await memory_service.get_or_create_conversation(user_id)
        recent_turns = await memory_service.get_recent_turns(conversation.id)

        self._alog.info(
            turn_id, "memory.loaded",
            memory_count=len(memories),
            memory_keys=[m.key for m in memories],
            conversation_id=conversation.id,
            recent_turns_count=len(recent_turns),
        )

        context = self._context_builder.build(
            user_id=user_id,
            current_message=message,
            memories=memories,
            recent_turns=recent_turns,
            conversation=conversation,
        )

        self._alog.info(
            turn_id, "context.built",
            system_context_length=len(context.to_system_context()),
            conversation_id=context.conversation_id,
        )

        # ── 4. Dependencies ───────────────────────────────────────────────
        deps = AgentDependencies(
            db=db,
            user_id=user_id,
            memory_service=memory_service,
            memory_config=self._memory_config,
            current_message_metadata=metadata,
        )

        # ── 4b. Guided-search shortcut ────────────────────────────────────
        shortcut_response = await self._maybe_handle_guided_search(
            user_id=user_id,
            message=message,
            deps=deps,
            memory_service=memory_service,
            memories=memories,
            metadata=metadata,
            turn_id=turn_id,
        )
        if shortcut_response is not None:
            self._alog.info(
                turn_id, "pipeline.shortcut",
                intent=shortcut_response.intent.value,
                response_preview=shortcut_response.message[:120],
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
            if context.conversation_id is not None:
                await memory_service.process_interaction(
                    user_id=user_id,
                    user_message=message,
                    assistant_response=shortcut_response.message,
                    conversation_id=context.conversation_id,
                    intent=shortcut_response.intent.value,
                )
            await db.commit()
            return self._to_orchestrator_response(shortcut_response)

        # ── 5. Run agent ──────────────────────────────────────────────────
        system_context = context.to_system_context()
        user_prompt = self._build_user_prompt(message, system_context, metadata)

        self._alog.info(
            turn_id, "agent.run",
            model=self._settings.openai_model,
            user_prompt_length=len(user_prompt),
            user_prompt_preview=user_prompt[:300],
        )

        try:
            result = await router_agent.run(
                user_prompt,
                deps=deps,
                model=f"openai-chat:{self._settings.openai_model}",
            )
            agent_response: AgentResponse = result.output
            self._alog.info(
                turn_id, "agent.result",
                intent=agent_response.intent.value,
                confidence=agent_response.confidence,
                requires_action=agent_response.requires_action,
                entities=agent_response.entities,
                response_preview=agent_response.message[:200],
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
        except Exception as exc:
            logger.exception("Agent run failed for user %s: %s", user_id, exc)
            self._alog.error(
                turn_id, "agent.error",
                exception=str(exc),
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
            await self._rollback_quietly(db, user_id, "agent run failure")
            agent_response = AgentResponse(
                intent=Intent.CONVERSACION_GENERAL,
                message="Disculpá, hubo un problema procesando tu mensaje. Intentá de nuevo.",
                confidence=0.0,
            )
            return self._to_orchestrator_response(agent_response)

        # ── 6-8. Post-process + persist ───────────────────────────────────
        try:
            if context.conversation_id is not None:
                await memory_service.process_interaction(
                    user_id=user_id,
                    user_message=message,
                    assistant_response=agent_response.message,
                    conversation_id=context.conversation_id,
                    intent=agent_response.intent.value,
                )

            await db.commit()
            self._alog.info(
                turn_id, "pipeline.persisted",
                conversation_id=context.conversation_id,
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
        except Exception as exc:
            logger.exception("Post-processing failed for user %s: %s", user_id, exc)
            self._alog.error(
                turn_id, "pipeline.persist_error",
                exception=str(exc),
            )
            await self._rollback_quietly(db, user_id, "post-processing failure")

        # ── 9. Return ─────────────────────────────────────────────────────
        return self._to_orchestrator_response(agent_response)

    @staticmethod
    async def _rollback_quietly(db: AsyncSession, user_id: str, reason: str) -> None:
        """Best-effort rollback to recover from an aborted SQLAlchemy session."""
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback failed for user %s after %s", user_id, reason)

    @staticmethod
    def _to_orchestrator_response(agent_response: AgentResponse) -> OrchestratorResponse:
        """Translate the agent output contract into the bot-facing response contract."""
        return OrchestratorResponse(
            message=agent_response.message,
            intent=agent_response.intent.value,
            confidence=agent_response.confidence,
            entities=agent_response.entities,
            requires_action=agent_response.requires_action,
            metadata=agent_response.metadata,
        )

    def _build_memory_service(self, db: AsyncSession) -> MemoryService:
        """Create the request-scoped memory service facade."""
        return MemoryService(
            session=db,
            extractor=MemoryExtractor(self._openai, model="gpt-4o-mini"),
            summarizer=MemorySummarizer(self._openai, model="gpt-4o-mini"),
            config=self._memory_config,
        )

    @staticmethod
    def _build_user_prompt(
        message: str,
        system_context: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        sections: list[str] = []
        if system_context:
            sections.append(system_context)
        if metadata:
            sections.append(f"## Metadata del mensaje\n{metadata}")
        sections.append(f"Mensaje actual del usuario: {message}")
        return "\n\n---\n".join(sections)

    async def _maybe_handle_guided_search(
        self,
        *,
        user_id: str,
        message: str,
        deps: AgentDependencies,
        memory_service: MemoryService,
        memories: list[MemoryRead],
        metadata: dict[str, Any] | None,
        turn_id: str = "",
    ) -> AgentResponse | None:
        """Handle the simple provider-search flow without invoking the agent."""
        state_repo = EstadoRepository(deps.db)
        raw_state = await state_repo.get(user_id)
        state = raw_state if raw_state.get("estado") == SEARCH_STATE_NAME else {}

        current_location = self._location_from_metadata(metadata)
        stored_location = self._location_from_memories(memories)
        search_location = current_location or stored_location
        detail = state.get("detalle")

        paso = state.get("paso")
        self._alog.info(
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
                self._alog.info(turn_id, "guided_search.awaiting_need.no_match", message_preview=message[:80])
                return None
            if search_location is None:
                self._alog.info(turn_id, "guided_search.awaiting_need.need_zone", rubro=rubro)
                await self._save_search_state(state_repo, user_id, "awaiting_zone", rubro)
                return self._build_location_request_response(rubro)
            self._alog.info(turn_id, "guided_search.awaiting_need.search_ready", rubro=rubro, location=self._location_label(search_location))
            return await self._build_search_results_response(
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
                self._alog.info(turn_id, "guided_search.awaiting_zone.from_metadata", rubro=rubro)
            elif typed_zone:
                search_location = {"zona": typed_zone}
                self._alog.info(turn_id, "guided_search.awaiting_zone.from_text", rubro=rubro, typed_zone=typed_zone)
            elif stored_location is not None:
                search_location = stored_location
                self._alog.info(turn_id, "guided_search.awaiting_zone.from_memory", rubro=rubro)
            else:
                self._alog.info(turn_id, "guided_search.awaiting_zone.no_zone_found", rubro=rubro)
                return None
            return await self._build_search_results_response(
                deps=deps,
                memory_service=memory_service,
                rubro=rubro,
                location=search_location,
                detail=detail,
            )

        if not self._is_search_start(message, metadata):
            return None

        rubro = self._extract_search_need(message, allow_plain=False)
        if rubro is None:
            self._alog.info(turn_id, "guided_search.fresh.no_rubro", message_preview=message[:80])
            await self._save_search_state(state_repo, user_id, "awaiting_need")
            return AgentResponse(
                intent=Intent.BUSCAR_SERVICIO,
                message="Decime qué servicio necesitás y te busco opciones cerca.",
                confidence=1.0,
                requires_action=False,
            )

        if search_location is None:
            self._alog.info(turn_id, "guided_search.fresh.need_zone", rubro=rubro)
            await self._save_search_state(state_repo, user_id, "awaiting_zone", rubro)
            return self._build_location_request_response(rubro)

        self._alog.info(turn_id, "guided_search.fresh.search_ready", rubro=rubro, location=self._location_label(search_location))
        return await self._build_search_results_response(
            deps=deps,
            memory_service=memory_service,
            rubro=rubro,
            location=search_location,
            detail=detail,
        )

    async def _build_search_results_response(
        self,
        *,
        deps: AgentDependencies,
        memory_service: MemoryService,
        rubro: str,
        location: dict[str, Any],
        detail: str | None,
    ) -> AgentResponse:
        """Run provider search directly and format the reply message.

        (C) If the location has only a text zone (no coordinates), resolve it
        via geocoding here so that buscar_prestadores receives lat/lon and
        avoids a redundant geocode call inside _resolve_search_origin. The
        resolved coordinates are also persisted for future search turns.
        """
        # (C) Resolve text-only zone to coordinates early
        location = await self._enrich_location_with_coords(location)

        params = BuscarPrestadoresInput(
            rubro=rubro,
            zona=self._location_label(location),
            lat=location.get("lat"),
            lon=location.get("lon"),
            limit=5,
        )
        ctx = SimpleNamespace(deps=deps)
        self._alog.info("", "shortcut.search_run",
                         rubro=rubro, zona=params.zona, lat=params.lat, lon=params.lon)
        providers = await buscar_prestadores(ctx, params)
        self._alog.info("", "shortcut.search_result",
                         rubro=rubro, provider_count=len(providers),
                         provider_names=[p.get("nombre") for p in providers[:5]])

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
                entities={"rubro": rubro, "zona": location_label, "detalle": detail},
                requires_action=True,
            )

        location_label = self._location_label(location) or "tu ubicación"
        return AgentResponse(
            intent=Intent.BUSCAR_SERVICIO,
            message=self._format_provider_results(rubro, location_label, providers),
            confidence=1.0,
            entities={"rubro": rubro, "zona": location_label, "detalle": detail},
            requires_action=True,
            metadata={"providers": providers},
        )

    @staticmethod
    async def _enrich_location_with_coords(
        location: dict[str, Any],
    ) -> dict[str, Any]:
        """(C) If a location has a text zone but no coordinates, resolve via geocoding.

        Returns the location dict potentially enriched with lat/lon.
        Geocoding results are cached for 24h (see geocoding.py).
        """
        if location.get("lat") is not None and location.get("lon") is not None:
            return location

        zona = AIOrchestrator._location_label(location)
        if not zona:
            return location

        geocoded = await geocode_text_location(zona)
        latitude = geocoded.get("lat")
        longitude = geocoded.get("lon")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            location["lat"] = float(latitude)
            location["lon"] = float(longitude)
            # Also save any city/barrio the geocoder provided
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

    async def _store_search_location_if_available(
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
        zona = self._location_label(location)

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
        if zona:
            await memory_service.upsert_memory(
                user_id,
                LOCATION_MEMORY_KEYS["zona"],
                zona,
                0.85,
            )

    @staticmethod
    async def _save_search_state(
        state_repo: EstadoRepository,
        user_id: str,
        paso: str,
        rubro: str | None = None,
        detalle: str | None = None,
    ) -> None:
        """Persist the current guided-search state for the next turn."""
        await state_repo.save(
            user_id,
            {
                "estado": SEARCH_STATE_NAME,
                "paso": paso,
                "rubro": rubro,
                "zona": None,
                "detalle": detalle,
            },
        )

    @staticmethod
    def _is_search_start(
        message: str,
        metadata: dict[str, Any] | None,
    ) -> bool:
        """Return True when the message explicitly starts a service search."""
        if metadata and metadata.get("selected_id") == SEARCH_BUTTON_ID:
            return True

        normalized = AIOrchestrator._normalize_message(message)
        if normalized in {
            "quiero buscar servicios",
            "busco servicios",
            "buscar servicios",
            "contratar un servicio",
            "contratar servicio",
        }:
            return True
        return any(normalized.startswith(prefix) for prefix in SEARCH_PREFIXES)

    @staticmethod
    def _extract_search_need(message: str, allow_plain: bool) -> str | None:
        """Extract the requested trade from a search message or short follow-up."""
        normalized = AIOrchestrator._normalize_message(message)
        candidate = normalized
        for prefix in SEARCH_PREFIXES:
            if normalized.startswith(prefix):
                candidate = normalized[len(prefix) :]
                break
        else:
            if not allow_plain:
                return None

        candidate = re.split(r"\b(?: en | por | para | cerca de )\b", candidate, maxsplit=1)[0]
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
    def _extract_zone_reply(message: str) -> str | None:
        """Normalize a location follow-up such as a barrio or locality name."""
        normalized = AIOrchestrator._normalize_message(message)
        for prefix in ZONE_REPLY_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break

        if not normalized or AIOrchestrator._is_search_start(message, None):
            return None

        words = normalized.split()
        if not words or len(words) > 6:
            return None
        return message.strip(" .,!?:;")

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

        zona_parts = [part for part in [barrio, ciudad] if isinstance(part, str) and part]
        return {
            "lat": float(latitude) if isinstance(latitude, (int, float)) else None,
            "lon": float(longitude) if isinstance(longitude, (int, float)) else None,
            "ciudad": ciudad if isinstance(ciudad, str) else None,
            "barrio": barrio if isinstance(barrio, str) else None,
            "zona": ", ".join(zona_parts) if zona_parts else None,
        }

    @staticmethod
    def _location_from_memories(memories: list[MemoryRead]) -> dict[str, Any] | None:
        """Load the last shared location from persistent user memory."""
        values = {memory.key: memory.value for memory in memories}
        lat = AIOrchestrator._parse_float(values.get(LOCATION_MEMORY_KEYS["lat"]))
        lon = AIOrchestrator._parse_float(values.get(LOCATION_MEMORY_KEYS["lon"]))
        ciudad = values.get(LOCATION_MEMORY_KEYS["ciudad"])
        barrio = values.get(LOCATION_MEMORY_KEYS["barrio"])
        zona = values.get(LOCATION_MEMORY_KEYS["zona"])
        if lat is None and lon is None and not ciudad and not barrio and not zona:
            return None
        return {
            "lat": lat,
            "lon": lon,
            "ciudad": ciudad,
            "barrio": barrio,
            "zona": zona,
        }

    @staticmethod
    def _location_label(location: dict[str, Any]) -> str | None:
        """Return the best human-readable label for a search location."""
        zona = location.get("zona")
        if isinstance(zona, str) and zona:
            return zona
        parts = [location.get("barrio"), location.get("ciudad")]
        labels = [part for part in parts if isinstance(part, str) and part]
        return ", ".join(labels) if labels else None

    @staticmethod
    def _format_provider_results(
        rubro: str,
        location_label: str,
        providers: list[dict[str, Any]],
    ) -> str:
        """Render the provider shortlist for WhatsApp-friendly delivery."""
        lines = [f"Encontré hasta 5 {rubro} cerca de {location_label}:"]
        for index, provider in enumerate(providers[:5], start=1):
            rubros = ", ".join(provider.get("rubros") or [])
            zone_parts = [provider.get("barrio"), provider.get("ciudad"), provider.get("zona")]
            zone = next(
                (
                    part
                    for part in zone_parts
                    if isinstance(part, str) and part
                ),
                "Zona no informada",
            )
            verified = "Verificado" if provider.get("badge_verificado") else "Base"
            distance = provider.get("distance_km")
            distance_text = (
                f" - {distance:.1f} km" if isinstance(distance, (int, float)) else ""
            )
            lines.append(
                f"{index}. {provider['nombre']} | {rubros or rubro} | {zone} | "
                f"{verified}{distance_text}"
            )
        return "\n".join(lines)

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
