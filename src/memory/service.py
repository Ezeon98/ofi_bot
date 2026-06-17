"""MemoryService — the single public interface for all memory operations.

Coordinates MemoryRepository, ConversationRepository, MemoryExtractor and
MemorySummarizer. Called exclusively by the AIOrchestrator.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.extractor import MemoryExtractor
from src.memory.models import MemoryConfig, MemoryEntry
from src.memory.repository import ConversationRepository, MemoryRepository
from src.memory.schemas import ConversationRead, ConversationTurnRead, MemoryRead, MemoryUpsert
from src.memory.summarizer import MemorySummarizer

logger = logging.getLogger(__name__)


class MemoryService:
    """Orchestrates memory retrieval, persistence and housekeeping."""

    def __init__(
        self,
        session: AsyncSession,
        extractor: MemoryExtractor,
        summarizer: MemorySummarizer,
        config: MemoryConfig,
    ) -> None:
        self._memories = MemoryRepository(session)
        self._conversations = ConversationRepository(session)
        self._extractor = extractor
        self._summarizer = summarizer
        self._config = config

    # ── Public read interface ────────────────────────────────────────────

    async def get_memories(self, user_id: str) -> list[MemoryRead]:
        if not self._config.enabled:
            return []
        return await self._memories.get_by_user(
            user_id,
            min_importance=self._config.importance_threshold,
            limit=self._config.max_memories,
        )

    async def get_or_create_conversation(self, user_id: str) -> ConversationRead:
        return await self._conversations.get_or_create_active(user_id)

    async def get_recent_turns(
        self, conversation_id: int, limit: int = 20
    ) -> list[ConversationTurnRead]:
        return await self._conversations.get_recent_turns(conversation_id, limit)

    # ── Public write interface ───────────────────────────────────────────

    async def save_turn(
        self,
        conversation_id: int,
        role: str,
        content: str,
        intent: str | None = None,
    ) -> ConversationTurnRead:
        return await self._conversations.add_turn(conversation_id, role, content, intent)

    async def upsert_memory(self, user_id: str, key: str, value: str, importance: float = 0.7) -> MemoryRead:
        return await self._memories.upsert(user_id, MemoryUpsert(key=key, value=value, importance=importance))

    async def process_interaction(
        self,
        user_id: str,
        user_message: str,
        assistant_response: str,
        conversation_id: int,
        intent: str | None = None,
    ) -> None:
        """Post-turn pipeline: extract facts, save turns, prune, maybe summarise."""
        if not self._config.enabled:
            return

        # 1. Persist turns
        await self._conversations.add_turn(conversation_id, "user", user_message, intent)
        await self._conversations.add_turn(conversation_id, "assistant", assistant_response)

        # 2. Extract and upsert facts
        facts = await self._extractor.extract(user_message, assistant_response)
        for fact in facts:
            if fact.importance >= self._config.importance_threshold:
                await self._memories.upsert(
                    user_id,
                    MemoryUpsert(key=fact.key, value=fact.value, importance=fact.importance),
                )

        # 3. Prune excess memories
        count = await self._memories.count_by_user(user_id)
        if count > self._config.max_memories:
            await self._memories.drop_least_important(user_id, self._config.max_memories)

        # 4. Summarise if turn count exceeds threshold
        turn_count = await self._conversations.count_turns(conversation_id)
        if turn_count >= self._config.summarize_after:
            await self._maybe_summarize(conversation_id, user_id)

    # ── Internal ─────────────────────────────────────────────────────────

    async def _maybe_summarize(self, conversation_id: int, user_id: str) -> None:
        keep_last = 10  # ponytail: keep a short recency window after summarising
        turns = await self._conversations.get_recent_turns(conversation_id, limit=self._config.summarize_after)
        if not turns:
            return
        raw = [{"role": t.role, "content": t.content} for t in turns]
        summary = await self._summarizer.summarize(raw)
        await self._conversations.update_summary(conversation_id, summary)
        await self._conversations.delete_turns_before_offset(conversation_id, keep_last)
        logger.info("Summarised conversation %d for user %s", conversation_id, user_id)
