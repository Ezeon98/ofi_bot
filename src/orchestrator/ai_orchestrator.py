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

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import (
    AgentResponse,
    Intent,
    Message,
    MessageAction,
    ReplyButton,
)
import src.agents.router_agent as router_agents
from src.application.services.provider_profile_service import (
    ProviderProfileService,
)
from src.application.services.provider_registration_service import (
    OFFER_SERVICES_BUTTON_ID,
    ProviderRegistrationService,
)
from src.application.services.provider_search_service import (
    SEARCH_BUTTON_ID,
    ProviderSearchService,
)
from src.context.builder import ContextBuilder
from src.infrastructure.config import Settings
from src.infrastructure.database.repositories.estado import (
    EstadoRepository,
    MODE_PROVIDER_PROFILE,
    MODE_PROVIDER_SEARCH,
)
from src.infrastructure.database.repositories.usuario import UsuarioRepository
from src.infrastructure.external.openai_client import build_openai_client
from src.memory.extractor import MemoryExtractor
from src.memory.models import MemoryConfig
from src.memory.service import MemoryService
from src.memory.summarizer import MemorySummarizer
from src.application.services.system_fallback_service import SystemFallbackService
from src.utils.agent_logger import AgentLogger

logger = logging.getLogger(__name__)

MODE_SWITCH_CONFIRM_YES = {"si", "sí"}
MODE_SWITCH_CONFIRM_NO = {"no"}
router_agent = getattr(router_agents, "router_agent", None)


class OrchestratorResponse(BaseModel):
    """What the bot layer receives — channel-agnostic."""

    message: str
    source: str = Field(description="Pipeline branch that produced the response: shortcut or llm")
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Additional messages to send individually. Each dict has a 'text' key "
            "and optionally an 'action' key with 'type', 'label', 'url'."
        ),
    )
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
        self._openai = build_openai_client(settings)
        self._context_builder = ContextBuilder(max_tokens=self._memory_config.max_tokens)
        self._alog = AgentLogger(enabled=settings.agent_logging_enabled)
        self._provider_registration = ProviderRegistrationService(
            memory_config=self._memory_config,
            agent_logger=self._alog,
        )
        self._provider_profile = ProviderProfileService(
            agent_logger=self._alog,
        )
        self._provider_search = ProviderSearchService(
            memory_config=self._memory_config,
            agent_logger=self._alog,
            openai_client=self._openai,
            openai_model=settings.openai_model,
        )
        self._system_fallback = SystemFallbackService(
            client=self._openai,
            model=settings.openai_model,
        )

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
            turn_id,
            "pipeline.start",
            user_id=user_id,
            message_length=len(message),
            message_preview=message[:120],
            metadata_keys=list(metadata.keys()) if metadata else None,
        )

        db_lock = asyncio.Lock()
        memory_service = self._build_memory_service(db, db_lock)

        # Resolve the integer DB id once for the whole pipeline.
        usuario_id = await self._resolve_usuario_id(db, user_id)
        if usuario_id is None:
            logger.warning("No usuario found for telefono %s", user_id)
            return OrchestratorResponse(
                message="No pudimos identificarte. Por favor escribinos de nuevo.",
                source="error",
                intent=Intent.CONVERSACION_GENERAL.value,
                confidence=0.0,
            )

        await self._provider_search.store_search_location_if_available(
            memory_service,
            usuario_id,
            metadata,
        )

        # ── 1-3. Memory + history + context ──────────────────────────────
        memories = await memory_service.get_memories(usuario_id)
        conversation = await memory_service.get_or_create_conversation(usuario_id)
        recent_turns = await memory_service.get_recent_turns(conversation.id)

        self._alog.info(
            turn_id,
            "memory.loaded",
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
            turn_id,
            "context.built",
            system_context_length=len(context.to_system_context()),
            conversation_id=context.conversation_id,
        )

        # ── 4. Dependencies ───────────────────────────────────────────────
        deps = AgentDependencies(
            db=db,
            db_lock=db_lock,
            user_id=user_id,
            usuario_id=usuario_id,
            memory_service=memory_service,
            memory_config=self._memory_config,
            current_message_metadata=metadata,
        )

        state_repo = self._build_state_repo(db)
        mode_state = {
            "active_mode": None,
            "pending_mode": None,
            "pending_confirmation": False,
            "flows": {},
        }
        if state_repo is not None:
            mode_state = await state_repo.get_mode(user_id)
            mode_resolution = self._resolve_requested_mode(
                message=message,
                metadata=metadata,
                active_mode=mode_state.get("active_mode"),
            )

            confirmation_response = await self._maybe_handle_mode_confirmation(
                state_repo=state_repo,
                user_id=user_id,
                message=message,
                mode_state=mode_state,
                requested_mode=mode_resolution["requested_mode"],
            )
            if confirmation_response is not None:
                await db.commit()
                return self._to_orchestrator_response(
                    confirmation_response,
                    source="mode_switch",
                )

            effective_mode = mode_resolution["effective_mode"]
            if effective_mode is not None and effective_mode != mode_state.get("active_mode"):
                await state_repo.save_mode(user_id, active_mode=effective_mode)
                mode_state["active_mode"] = effective_mode
        else:
            mode_resolution = self._resolve_requested_mode(
                message=message,
                metadata=metadata,
                active_mode=None,
            )
            effective_mode = None

        self._alog.info(
            turn_id,
            "pipeline.mode",
            active_mode=mode_state.get("active_mode"),
            pending_mode=mode_state.get("pending_mode"),
            pending_confirmation=mode_state.get("pending_confirmation"),
            requested_mode=mode_resolution.get("requested_mode"),
            effective_mode=effective_mode,
        )

        # ── 4b. Guided provider-registration shortcut ─────────────────────
        if effective_mode == MODE_PROVIDER_SEARCH:
            location_update_response = await self._provider_search.maybe_handle_location_update(
                user_id=user_id,
                message=message,
                deps=deps,
                memory_service=memory_service,
                metadata=metadata,
                turn_id=turn_id,
            )
            if location_update_response is not None:
                if state_repo is not None:
                    await state_repo.save_mode(
                        user_id,
                        active_mode=MODE_PROVIDER_SEARCH,
                    )
                if context.conversation_id is not None:
                    await memory_service.process_interaction(
                        user_id=usuario_id,
                        user_message=message,
                        assistant_response=location_update_response.message,
                        conversation_id=context.conversation_id,
                        intent=location_update_response.intent.value,
                    )
                await db.commit()
                return self._to_orchestrator_response(
                    location_update_response,
                    source="shortcut",
                )

            guided_search_response = await self._provider_search.maybe_handle_guided_search(
                user_id=user_id,
                message=message,
                metadata=metadata,
                deps=deps,
                memory_service=memory_service,
                memories=memories,
                turn_id=turn_id,
            )
            if guided_search_response is not None:
                if state_repo is not None:
                    await state_repo.save_mode(
                        user_id,
                        active_mode=MODE_PROVIDER_SEARCH,
                    )
                if context.conversation_id is not None:
                    await memory_service.process_interaction(
                        user_id=usuario_id,
                        user_message=message,
                        assistant_response=guided_search_response.message,
                        conversation_id=context.conversation_id,
                        intent=guided_search_response.intent.value,
                    )
                await db.commit()
                return self._to_orchestrator_response(
                    guided_search_response,
                    source="shortcut",
                )

        registration_response = None
        if effective_mode == MODE_PROVIDER_PROFILE:
            registration_response = await self._provider_registration.maybe_handle_registration(
                user_id=user_id,
                message=message,
                deps=deps,
                memory_service=memory_service,
                metadata=metadata,
                turn_id=turn_id,
            )
        if registration_response is not None:
            if state_repo is not None:
                await state_repo.save_mode(
                    user_id,
                    active_mode=MODE_PROVIDER_PROFILE,
                )
            self._alog.info(
                turn_id,
                "pipeline.shortcut",
                intent=registration_response.intent.value,
                response_preview=registration_response.message[:120],
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
            if context.conversation_id is not None:
                await memory_service.process_interaction(
                    user_id=usuario_id,
                    user_message=message,
                    assistant_response=registration_response.message,
                    conversation_id=context.conversation_id,
                    intent=registration_response.intent.value,
                )
            await db.commit()
            return self._to_orchestrator_response(
                registration_response,
                source="shortcut",
            )

        profile_update_response = None
        if effective_mode == MODE_PROVIDER_PROFILE:
            profile_update_response = await self._provider_profile.maybe_handle_profile_update(
                user_id=user_id,
                message=message,
                metadata=metadata,
                deps=deps,
                turn_id=turn_id,
            )
        if profile_update_response is not None:
            if state_repo is not None:
                await state_repo.save_mode(
                    user_id,
                    active_mode=MODE_PROVIDER_PROFILE,
                )
            if context.conversation_id is not None:
                await memory_service.process_interaction(
                    user_id=usuario_id,
                    user_message=message,
                    assistant_response=profile_update_response.message,
                    conversation_id=context.conversation_id,
                    intent=profile_update_response.intent.value,
                )
            await db.commit()
            return self._to_orchestrator_response(
                profile_update_response,
                source="shortcut",
            )

        # ── 5. Run agent ──────────────────────────────────────────────────
        system_context = context.to_system_context()
        prompt_metadata = dict(metadata or {})
        if effective_mode is not None:
            prompt_metadata["active_mode"] = effective_mode
        llm_agent, agent_name = self._select_llm_agent(effective_mode)
        prompt_metadata["agent_name"] = agent_name
        deps.current_message_metadata = prompt_metadata
        user_prompt = self._build_user_prompt(message, system_context, prompt_metadata)

        self._alog.info(
            turn_id,
            "agent.run",
            agent_name=agent_name,
            active_mode=effective_mode,
            model=self._settings.openai_model,
            user_prompt_length=len(user_prompt),
            user_prompt_preview=user_prompt[:300],
        )

        try:
            result = await llm_agent.run(
                user_prompt,
                deps=deps,
                model=f"openai-chat:{self._settings.openai_model}",
            )
            agent_response: AgentResponse = result.output
            tool_providers = self._provider_search.extract_provider_results_from_run_messages(
                result.new_messages()
            )
            self._alog.info(
                turn_id,
                "agent.result",
                agent_name=agent_name,
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
                turn_id,
                "agent.error",
                exception=str(exc),
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
            await self._rollback_quietly(db, user_id, "agent run failure")
            agent_response = AgentResponse(
                intent=Intent.CONVERSACION_GENERAL,
                message="Disculpá, hubo un problema procesando tu mensaje. Intentá de nuevo.",
                confidence=0.0,
            )
            return self._to_orchestrator_response(agent_response, source="llm_error")

        if agent_response.intent == Intent.CONSULTAR_SISTEMA:
            agent_response = await self._answer_system_question(
                agent_response=agent_response,
                message=message,
                system_context=system_context,
                metadata=metadata,
                turn_id=turn_id,
            )

        # ── 6-7. Post-process agent response (reformat providers into multi-message) ──
        agent_response = await self._provider_search.maybe_reformat_provider_response(
            agent_response,
            rubro=agent_response.entities.get("rubro") if agent_response.entities else None,
            providers=tool_providers,
        )

        # ── 7b. Persist search entities to memory (barrio, ciudad, rubro) ────
        # The shortcut path persists these via _persist_search_location, but the
        # LLM agent path must also persist them so they're available for future
        # searches. The MemoryExtractor is explicitly told to skip search_* keys.
        if agent_response.intent == Intent.ACTUALIZAR_UBICACION and agent_response.entities:
            # User is updating their location — persist it without searching
            location_entities = agent_response.entities
            location = {}
            barrio = location_entities.get("barrio")
            ciudad = location_entities.get("ciudad")
            if isinstance(barrio, str) and barrio:
                location["barrio"] = barrio
            if isinstance(ciudad, str) and ciudad:
                location["ciudad"] = ciudad
            if location:
                await self._provider_search.persist_search_location(
                    memory_service,
                    usuario_id,
                    location,
                )

        if agent_response.intent == Intent.BUSCAR_SERVICIO and agent_response.entities:
            search_entities = agent_response.entities
            location = {}
            barrio = search_entities.get("barrio")
            ciudad = search_entities.get("ciudad")
            if isinstance(barrio, str) and barrio:
                location["barrio"] = barrio
            if isinstance(ciudad, str) and ciudad:
                location["ciudad"] = ciudad
            if location:
                await self._provider_search.persist_search_location(
                    memory_service,
                    usuario_id,
                    location,
                )
            # Persist rubro so future conversations know what the user last searched
            rubro = search_entities.get("rubro")
            if isinstance(rubro, str) and rubro:
                await memory_service.upsert_memory(
                    usuario_id,
                    "rubro",
                    rubro,
                    importance=0.8,
                )

        inferred_mode = self._mode_from_agent_intent(agent_response.intent)
        if inferred_mode is not None and state_repo is not None:
            await state_repo.save_mode(user_id, active_mode=inferred_mode)

        # ── 8. Persist ────────────────────────────────────────────────────
        try:
            if context.conversation_id is not None:
                await memory_service.process_interaction(
                    user_id=usuario_id,
                    user_message=message,
                    assistant_response=agent_response.message,
                    conversation_id=context.conversation_id,
                    intent=agent_response.intent.value,
                )

            await db.commit()
            self._alog.info(
                turn_id,
                "pipeline.persisted",
                conversation_id=context.conversation_id,
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
        except Exception as exc:
            logger.exception("Post-processing failed for user %s: %s", user_id, exc)
            self._alog.error(
                turn_id,
                "pipeline.persist_error",
                exception=str(exc),
            )
            await self._rollback_quietly(db, user_id, "post-processing failure")

        # ── 9. Return ─────────────────────────────────────────────────────
        return self._to_orchestrator_response(agent_response, source="llm")

    @staticmethod
    async def _rollback_quietly(db: AsyncSession, user_id: str, reason: str) -> None:
        """Best-effort rollback to recover from an aborted SQLAlchemy session."""
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback failed for user %s after %s", user_id, reason)

    @staticmethod
    def _to_orchestrator_response(
        agent_response: AgentResponse,
        *,
        source: str,
    ) -> OrchestratorResponse:
        """Translate the agent output contract into the bot-facing response contract."""
        return OrchestratorResponse(
            message=agent_response.message,
            source=source,
            messages=[
                {
                    "text": m.text,
                    "action": (
                        {
                            "type": m.action.type,
                            "label": m.action.label,
                            "url": m.action.url,
                            "buttons": [
                                {"id": button.id, "title": button.title}
                                for button in m.action.buttons
                            ],
                        }
                        if m.action
                        else None
                    ),
                }
                for m in agent_response.messages
            ],
            intent=agent_response.intent.value,
            confidence=agent_response.confidence,
            entities=agent_response.entities,
            requires_action=agent_response.requires_action,
            metadata=agent_response.metadata,
        )

    def _build_memory_service(
        self,
        db: AsyncSession,
        db_lock: asyncio.Lock | None = None,
    ) -> MemoryService:
        """Create the request-scoped memory service facade."""
        return MemoryService(
            session=db,
            extractor=MemoryExtractor(self._openai, model="gpt-4o-mini"),
            summarizer=MemorySummarizer(self._openai, model="gpt-4o-mini"),
            config=self._memory_config,
            db_lock=db_lock,
        )

    @staticmethod
    def _build_state_repo(db: AsyncSession) -> EstadoRepository | None:
        """Return a state repository only when the session supports SQL reads."""
        if not hasattr(db, "execute"):
            return None
        return EstadoRepository(db)

    async def _answer_system_question(
        self,
        *,
        agent_response: AgentResponse,
        message: str,
        system_context: str,
        metadata: dict[str, Any] | None,
        turn_id: str,
    ) -> AgentResponse:
        """Delegate product questions to the documentation-backed agent."""
        try:
            answer = await self._system_fallback.answer(
                question=message,
                system_context=system_context,
                metadata=metadata,
            )
        except Exception as exc:
            logger.exception(
                "System fallback agent failed for question %r: %s",
                message,
                exc,
            )
            self._alog.error(
                turn_id,
                "system_fallback.error",
                exception=str(exc),
            )
            return agent_response

        return AgentResponse(
            intent=Intent.CONSULTAR_SISTEMA,
            message=answer,
            messages=[],
            confidence=agent_response.confidence,
            entities=agent_response.entities,
            requires_action=False,
            metadata=agent_response.metadata,
        )

    @staticmethod
    async def _resolve_usuario_id(db: AsyncSession, telefono: str) -> int | None:
        """Map the inbound phone number to usuarios.id for internal FK usage."""
        return await UsuarioRepository(db).get_id_by_telefono(telefono)

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

    @staticmethod
    def _select_llm_agent(
        effective_mode: str | None,
    ) -> tuple[Any, str]:
        """Choose the specialized agent that matches the current top-level mode."""
        default_agent = AIOrchestrator._agent_with_run(
            getattr(router_agents, "provider_search_agent", None),
            router_agent,
        )
        if effective_mode == MODE_PROVIDER_PROFILE:
            return (
                AIOrchestrator._agent_with_run(
                    getattr(router_agents, "provider_profile_agent", None),
                    default_agent,
                ),
                "provider_profile_agent",
            )
        if effective_mode == MODE_PROVIDER_SEARCH:
            return (
                AIOrchestrator._agent_with_run(
                    getattr(router_agents, "provider_search_agent", None),
                    default_agent,
                ),
                "provider_search_agent",
            )
        return default_agent, "provider_search_agent"

    @staticmethod
    def _agent_with_run(preferred: Any, fallback: Any) -> Any:
        """Return the first agent-like object that exposes an async run method."""
        if hasattr(preferred, "run"):
            return preferred
        return fallback

    @staticmethod
    def _mode_from_agent_intent(intent: Intent) -> str | None:
        """Map agent-level intents back to the sticky top-level chat mode."""
        if intent in {
            Intent.REGISTRAR_PRESTADOR,
            Intent.ACTUALIZAR_PERFIL,
        }:
            return MODE_PROVIDER_PROFILE
        if intent in {
            Intent.BUSCAR_SERVICIO,
            Intent.ACTUALIZAR_UBICACION,
        }:
            return MODE_PROVIDER_SEARCH
        return None

    def _resolve_requested_mode(
        self,
        *,
        message: str,
        metadata: dict[str, Any] | None,
        active_mode: str | None,
    ) -> dict[str, str | None]:
        """Infer whether this turn explicitly targets one of the two top-level modes."""
        requested_mode = None
        metadata_mode = None
        if metadata:
            raw_mode = metadata.get("requested_mode")
            if raw_mode in {MODE_PROVIDER_PROFILE, MODE_PROVIDER_SEARCH}:
                metadata_mode = raw_mode

        if metadata_mode is not None:
            requested_mode = metadata_mode
        elif ProviderRegistrationService._is_registration_start(message, metadata):
            requested_mode = MODE_PROVIDER_PROFILE
        elif ProviderSearchService._is_search_start(message, metadata):
            requested_mode = MODE_PROVIDER_SEARCH
        elif ProviderSearchService._extract_location_update(message) is not None:
            requested_mode = MODE_PROVIDER_SEARCH

        effective_mode = active_mode
        if effective_mode is None:
            effective_mode = requested_mode

        return {
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
        }

    async def _maybe_handle_mode_confirmation(
        self,
        *,
        state_repo: EstadoRepository,
        user_id: str,
        message: str,
        mode_state: dict[str, Any],
        requested_mode: str | None,
    ) -> AgentResponse | None:
        """Ask for confirmation before leaving the current sticky top-level mode."""
        active_mode = mode_state.get("active_mode")
        pending_mode = mode_state.get("pending_mode")
        pending_confirmation = bool(mode_state.get("pending_confirmation"))
        normalized = message.strip().lower()

        if pending_confirmation and isinstance(pending_mode, str):
            if normalized in MODE_SWITCH_CONFIRM_YES:
                await state_repo.save_mode(user_id, active_mode=pending_mode)
                return self._build_mode_changed_response(pending_mode)
            if normalized in MODE_SWITCH_CONFIRM_NO:
                await state_repo.clear_pending_mode(user_id)
                return self._build_mode_kept_response(active_mode)
            return self._build_mode_confirmation_response(
                current_mode=active_mode,
                target_mode=pending_mode,
            )

        if active_mode is not None and requested_mode is not None and requested_mode != active_mode:
            await state_repo.request_mode_switch(
                user_id,
                active_mode=active_mode,
                pending_mode=requested_mode,
            )
            return self._build_mode_confirmation_response(
                current_mode=active_mode,
                target_mode=requested_mode,
            )

        return None

    @staticmethod
    def _build_mode_confirmation_response(
        *,
        current_mode: str | None,
        target_mode: str,
    ) -> AgentResponse:
        """Create the SI/NO confirmation message used when switching modes."""
        current_label = AIOrchestrator._mode_label(current_mode)
        target_label = AIOrchestrator._mode_label(target_mode)
        return AgentResponse(
            intent=Intent.AYUDA,
            message=(
                f"Ahora estás en modo {current_label}. " f"¿Querés cambiar a modo {target_label}?"
            ),
            confidence=1.0,
            requires_action=False,
            metadata={
                "mode_switch": {
                    "current_mode": current_mode,
                    "target_mode": target_mode,
                    "status": "pending_confirmation",
                }
            },
            messages=[
                Message(
                    text="Respondé SI o NO para confirmar el cambio.",
                    action=MessageAction(
                        type="reply_buttons",
                        buttons=[
                            ReplyButton(id="mode_switch_yes", title="SI"),
                            ReplyButton(id="mode_switch_no", title="NO"),
                        ],
                    ),
                )
            ],
        )

    @staticmethod
    def _build_mode_changed_response(target_mode: str) -> AgentResponse:
        """Acknowledge a confirmed mode change and preserve inactive substate."""
        target_label = AIOrchestrator._mode_label(target_mode)
        return AgentResponse(
            intent=Intent.AYUDA,
            message=(f"Listo, cambié al modo {target_label}. " "Seguimos por ahí."),
            confidence=1.0,
            requires_action=False,
            metadata={
                "mode_switch": {
                    "target_mode": target_mode,
                    "status": "confirmed",
                }
            },
        )

    @staticmethod
    def _build_mode_kept_response(active_mode: str | None) -> AgentResponse:
        """Acknowledge that the user rejected the proposed mode switch."""
        active_label = AIOrchestrator._mode_label(active_mode)
        return AgentResponse(
            intent=Intent.AYUDA,
            message=(f"Perfecto, seguimos en modo {active_label}."),
            confidence=1.0,
            requires_action=False,
            metadata={
                "mode_switch": {
                    "active_mode": active_mode,
                    "status": "rejected",
                }
            },
        )

    @staticmethod
    def _mode_label(mode: str | None) -> str:
        """Render short human labels for the two top-level modes."""
        if mode == MODE_PROVIDER_PROFILE:
            return "perfil de prestador"
        if mode == MODE_PROVIDER_SEARCH:
            return "búsqueda de servicios"
        return "actual"

    # ── Old helper methods ─────────────────────────────────────────────────
    # These have been migrated to ProviderSearchService but kept as thin
    # wrappers for backward compatibility during the transition.
    # They will be removed after the migration is complete.
