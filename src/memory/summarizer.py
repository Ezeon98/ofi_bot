"""MemorySummarizer — condenses old conversation turns into a summary string
that is stored on the ConversationModel and replaces the raw turns.

Called by MemoryService when turn count exceeds MemoryConfig.summarize_after.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SUMMARISE_PROMPT = """\
Summarise the following conversation in 3–5 sentences in Spanish.
Focus on the user's needs, preferences, and any important information shared.
Be concise and factual.
"""


class MemorySummarizer:
    """Creates a text summary of a list of conversation turns."""

    def __init__(self, client: Any, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._model = model

    async def summarize(self, turns: list[dict[str, str]]) -> str:
        """
        Args:
            turns: list of {"role": "user"|"assistant", "content": "..."}

        Returns:
            A summary string in Spanish.
        """
        if not turns:
            return ""

        formatted = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in turns)
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SUMMARISE_PROMPT},
                    {"role": "user", "content": formatted},
                ],
                temperature=0.2,
                max_tokens=300,
            )
            return completion.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("Summarization failed: %s", exc)
            return formatted[:500]  # ponytail: degrade gracefully
