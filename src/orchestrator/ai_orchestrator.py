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
import time
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse, Intent
from src.agents.router_agent import router_agent
from src.context.builder import ContextBuilder
from src.infrastructure.config import Settings
from src.memory.extractor import MemoryExtractor
from src.memory.models import MemoryConfig
from src.memory.service import MemoryService
from src.memory.summarizer import MemorySummarizer
from src.application.services.provider_registration_service import ProviderRegistrationService
from src.application.services.provider_search_service import ProviderSearchService
from src.utils.agent_logger import AgentLogger

logger = logging.getLogger(__name__)


class OrchestratorResponse(BaseModel):
    """What the bot layer receives — channel-agnostic."""

    message: str
    source: str = Field(
        description="Pipeline branch that produced the response: shortcut or llm"
    )
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
        self._openai = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
        self._context_builder = ContextBuilder(max_tokens=self._memory_config.max_tokens)
        self._alog = AgentLogger(enabled=settings.agent_logging_enabled)
        self._provider_registration = ProviderRegistrationService(
            memory_config=self._memory_config,
            agent_logger=self._alog,
        )
        self._provider_search = ProviderSearchService(
            memory_config=self._memory_config,
            agent_logger=self._alog,
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
            turn_id, "pipeline.start",
            user_id=user_id,
            message_length=len(message),
            message_preview=message[:120],
            metadata_keys=list(metadata.keys()) if metadata else None,
        )

        memory_service = self._build_memory_service(db)
        await self._provider_search.store_search_location_if_available(
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

        # ── 4b. Guided provider-registration shortcut ─────────────────────
        registration_response = (
            await self._provider_registration.maybe_handle_registration(
                user_id=user_id,
                message=message,
                deps=deps,
                memory_service=memory_service,
                metadata=metadata,
                turn_id=turn_id,
            )
        )
        if registration_response is not None:
            self._alog.info(
                turn_id, "pipeline.shortcut",
                intent=registration_response.intent.value,
                response_preview=registration_response.message[:120],
                elapsed_ms=(time.monotonic() - _start) * 1000,
            )
            if context.conversation_id is not None:
                await memory_service.process_interaction(
                    user_id=user_id,
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

        # ── 4c. Guided-search shortcut ────────────────────────────────────
        shortcut_response = await self._provider_search.maybe_handle_guided_search(
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
            return self._to_orchestrator_response(shortcut_response, source="shortcut")

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
            tool_providers = self._provider_search.extract_provider_results_from_run_messages(
                result.new_messages()
            )
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
            return self._to_orchestrator_response(agent_response, source="llm_error")

        # ── 6-7. Post-process agent response (reformat providers into multi-message) ──
        agent_response = await self._provider_search.maybe_reformat_provider_response(
            agent_response,
            rubro=agent_response.entities.get("rubro") if agent_response.entities else None,
            providers=tool_providers,
        )

        # ── 8. Persist ────────────────────────────────────────────────────
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
                        {"type": m.action.type, "label": m.action.label, "url": m.action.url}
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

    # ── Old helper methods ─────────────────────────────────────────────────
    # These have been migrated to ProviderSearchService but kept as thin
    # wrappers for backward compatibility during the transition.
    # They will be removed after the migration is complete.
