"""Documentation-backed fallback agent for system/product questions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SYSTEM_FALLBACK_PROMPT = """\
Sos el agente documental de MiOficio.

Tu tarea es responder preguntas sobre el sistema usando SOLO la documentación
provista en el mensaje. Respondé en español rioplatense, de forma natural,
clara y breve.

Reglas:
- Contestá únicamente con información respaldada por la documentación provista.
- Si la documentación no alcanza para responder con certeza, decilo explícitamente.
- No inventes endpoints, planes, reglas, integraciones ni comportamientos.
- Si hay ambigüedad entre nombres del producto, explicá que es el mismo sistema.
- No des instrucciones internas de implementación salvo que el usuario pregunte
  por el funcionamiento técnico del sistema.
"""


class SystemFallbackService:
    """Answer product questions from the repository's core documentation."""

    def __init__(
        self,
        client: Any,
        model: str = "gpt-4o-mini",
        knowledge_base: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._knowledge_base = knowledge_base or self._load_knowledge_base()

    async def answer(
        self,
        *,
        question: str,
        system_context: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Return a natural-language answer grounded in project docs."""
        prompt_sections = [
            "## Documentación del sistema",
            self._knowledge_base,
        ]
        if system_context:
            prompt_sections.extend(
                [
                    "## Contexto conversacional",
                    system_context,
                ]
            )
        if metadata:
            prompt_sections.extend(
                [
                    "## Metadata del mensaje",
                    str(metadata),
                ]
            )
        prompt_sections.extend(
            [
                "## Pregunta del usuario",
                question,
            ]
        )

        completion = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_FALLBACK_PROMPT},
                {"role": "user", "content": "\n\n".join(prompt_sections)},
            ],
            temperature=0.2,
            max_tokens=700,
        )
        content = completion.choices[0].message.content or ""
        answer = content.strip()
        if answer:
            return answer
        return (
            "No encontré suficiente información documentada para responder eso "
            "con certeza en este momento."
        )

    @staticmethod
    def _load_knowledge_base() -> str:
        """Load the project docs that define system behavior and scope."""
        repo_root = Path(__file__).resolve().parents[3]
        doc_paths = [
            repo_root / "PROJECT_OVERVIEW.md",
            repo_root / "README.md",
        ]
        sections: list[str] = []
        for doc_path in doc_paths:
            if not doc_path.exists():
                continue
            content = doc_path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"### {doc_path.name}\n{content}")
        if sections:
            return "\n\n".join(sections)
        return "No hay documentación cargada del sistema."
