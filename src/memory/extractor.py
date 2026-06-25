"""MemoryExtractor — uses a lightweight LLM call to detect memorable facts
in a user/assistant exchange.

Responsibility: given a (user_message, assistant_response) pair, return a
list of (key, value, importance) facts worth persisting.

Keeps the extraction prompt separate from the main agent so it stays cheap
(small model, low latency).
"""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI
from pydantic import ValidationError

from src.memory.models import ExtractionResult, ExtractedFact

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
You are a memory extraction assistant. Given a conversation exchange, identify
facts about the USER that are worth remembering long-term.

Rules:
- Only extract facts explicitly stated or clearly implied by the USER.
- Use short snake_case keys (e.g. nombre, ciudad, rubro, preferencia_contacto).
- Assign importance 0.0–1.0 (1.0 = essential identity fact, 0.5 = useful preference).
- Skip trivial or transient information.
- NEVER use these reserved keys, they are managed by the system: \
search_barrio, search_ciudad, search_latitude, search_longitude.
- Return valid JSON: {"facts": [{"key": "...", "value": "...", "importance": 0.8}, ...]}.
- If nothing is worth remembering, return {"facts": []}.
"""


class MemoryExtractor:
    """Extracts memorable facts from a conversation exchange."""

    def __init__(self, client: AsyncOpenAI, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._model = model

    async def extract(
        self,
        user_message: str,
        assistant_response: str,
    ) -> list[ExtractedFact]:
        """Return facts extracted from this single exchange."""
        user_content = (
            f"USER: {user_message}\nASSISTANT: {assistant_response}"
        )
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _EXTRACTION_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.0,
                max_tokens=512,
            )
            raw = completion.choices[0].message.content or "{}"
            result = ExtractionResult.model_validate(json.loads(raw))
            return result.facts
        except (ValidationError, json.JSONDecodeError, Exception) as exc:
            logger.warning("Memory extraction failed: %s", exc)
            return []
