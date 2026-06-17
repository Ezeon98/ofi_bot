"""ContextBuilder — assembles the prompt context for the agent.

Combines:
  - User memories (persistent facts)
  - Conversation summary (if any)
  - Recent conversation turns
  - Current message

Respects a token budget so the context never exceeds MemoryConfig.max_tokens.
Token counting uses a rough 4-chars-per-token heuristic to avoid an extra
dependency; swap for tiktoken if precision matters.

ponytail: 4-char/token heuristic — ceiling is ~10% overcount on typical Spanish
          text. Upgrade path: `pip install tiktoken` and use `cl100k_base`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.memory.schemas import ConversationRead, ConversationTurnRead, MemoryRead


@dataclass
class AgentContext:
    """All contextual data the agent receives for a single turn."""

    user_id: str
    current_message: str
    memories: list[MemoryRead] = field(default_factory=list)
    recent_turns: list[ConversationTurnRead] = field(default_factory=list)
    conversation_summary: str | None = None
    conversation_id: int | None = None

    def to_system_context(self) -> str:
        """Render context as a readable block for the system prompt."""
        parts: list[str] = []

        if self.memories:
            mem_lines = "\n".join(f"  - {m.key}: {m.value}" for m in self.memories)
            parts.append(f"## Memoria del usuario\n{mem_lines}")

        if self.conversation_summary:
            parts.append(f"## Resumen de conversación previa\n{self.conversation_summary}")

        if self.recent_turns:
            history_lines = "\n".join(
                f"  {t.role.upper()}: {t.content}" for t in self.recent_turns
            )
            parts.append(f"## Historial reciente\n{history_lines}")

        return "\n\n".join(parts) if parts else ""


class ContextBuilder:
    """Builds an AgentContext respecting a token budget."""

    def __init__(self, max_tokens: int = 2000) -> None:
        self._max_tokens = max_tokens

    def build(
        self,
        user_id: str,
        current_message: str,
        memories: list[MemoryRead],
        recent_turns: list[ConversationTurnRead],
        conversation: ConversationRead | None,
    ) -> AgentContext:
        budget = self._max_tokens
        budget -= self._tokens(current_message)

        # 1. Fit as many memories as budget allows (already sorted by importance desc)
        selected_memories: list[MemoryRead] = []
        for m in memories:
            cost = self._tokens(f"{m.key}: {m.value}")
            if budget - cost < 0:
                break
            selected_memories.append(m)
            budget -= cost

        # 2. Fit summary
        summary = conversation.summary if conversation else None
        if summary:
            cost = self._tokens(summary)
            if budget - cost >= 0:
                budget -= cost
            else:
                summary = None

        # 3. Fit recent turns (most recent last, trim from the front)
        selected_turns: list[ConversationTurnRead] = []
        for turn in reversed(recent_turns):
            cost = self._tokens(turn.content)
            if budget - cost < 0:
                break
            selected_turns.insert(0, turn)
            budget -= cost

        return AgentContext(
            user_id=user_id,
            current_message=current_message,
            memories=selected_memories,
            recent_turns=selected_turns,
            conversation_summary=summary,
            conversation_id=conversation.id if conversation else None,
        )

    @staticmethod
    def _tokens(text: str) -> int:
        return max(1, len(text) // 4)
