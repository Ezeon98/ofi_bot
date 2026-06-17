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
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel
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

logger = logging.getLogger(__name__)


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

    async def process(
        self,
        *,
        user_id: str,
        message: str,
        db: AsyncSession,
        metadata: dict[str, Any] | None = None,
    ) -> OrchestratorResponse:
        """Full AI pipeline for one user message.

        Args:
            user_id: Channel-agnostic identifier (phone number, Telegram ID, etc.)
            message: Text of the message (audio should already be transcribed).
            db:      Injected AsyncSession for this request.
            metadata: Optional extra data (message_type, channel, etc.).

        Returns:
            OrchestratorResponse ready to be sent back through the channel.
        """
        memory_service = self._build_memory_service(db)

        # ── 1-3. Memory + history + context ──────────────────────────────
        memories = await memory_service.get_memories(user_id)
        conversation = await memory_service.get_or_create_conversation(user_id)
        recent_turns = await memory_service.get_recent_turns(conversation.id)

        context = self._context_builder.build(
            user_id=user_id,
            current_message=message,
            memories=memories,
            recent_turns=recent_turns,
            conversation=conversation,
        )

        # ── 4. Dependencies ───────────────────────────────────────────────
        deps = AgentDependencies(
            db=db,
            user_id=user_id,
            memory_service=memory_service,
            memory_config=self._memory_config,
        )

        # ── 5. Run agent ──────────────────────────────────────────────────
        system_context = context.to_system_context()
        user_prompt = self._build_user_prompt(message, system_context)

        try:
            result = await router_agent.run(
                user_prompt,
                deps=deps,
                model=f"openai:{self._settings.openai_model}",
            )
            agent_response: AgentResponse = result.output
        except Exception as exc:
            logger.exception("Agent run failed for user %s: %s", user_id, exc)
            agent_response = AgentResponse(
                intent=Intent.CONVERSACION_GENERAL,
                message="Disculpá, hubo un problema procesando tu mensaje. Intentá de nuevo.",
                confidence=0.0,
            )

        # ── 6-8. Post-process + persist ───────────────────────────────────
        if context.conversation_id is not None:
            await memory_service.process_interaction(
                user_id=user_id,
                user_message=message,
                assistant_response=agent_response.message,
                conversation_id=context.conversation_id,
                intent=agent_response.intent.value,
            )

        await db.commit()

        # ── 9. Return ─────────────────────────────────────────────────────
        return OrchestratorResponse(
            message=agent_response.message,
            intent=agent_response.intent.value,
            confidence=agent_response.confidence,
            entities=agent_response.entities,
            requires_action=agent_response.requires_action,
            metadata=agent_response.metadata,
        )

    def _build_memory_service(self, db: AsyncSession) -> MemoryService:
        return MemoryService(
            session=db,
            extractor=MemoryExtractor(self._openai, model="gpt-4o-mini"),
            summarizer=MemorySummarizer(self._openai, model="gpt-4o-mini"),
            config=self._memory_config,
        )

    @staticmethod
    def _build_user_prompt(message: str, system_context: str) -> str:
        if system_context:
            return f"{system_context}\n\n---\nMensaje actual del usuario: {message}"
        return message
