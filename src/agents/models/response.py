"""Pydantic models for agent input/output contracts.

The agent ALWAYS returns an AgentResponse — never raw text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Intent(StrEnum):
    BUSCAR_SERVICIO = "buscar_servicio"
    REGISTRAR_PRESTADOR = "registrar_prestador"
    ACTUALIZAR_PERFIL = "actualizar_perfil"
    ACTUALIZAR_UBICACION = "actualizar_ubicacion"
    CONSULTAR_ESTADO = "consultar_estado"
    CONTRATAR_SERVICIO = "contratar_servicio"
    UPGRADE_PLAN = "upgrade_plan"
    CANCELAR_PLAN = "cancelar_plan"
    AYUDA = "ayuda"
    CONVERSACION_GENERAL = "conversacion_general"


class MessageAction(BaseModel):
    """Action attached to a message (e.g. a CTA button)."""

    type: str = Field(description="Action type: 'cta_url'")
    label: str = Field(description="Button text, e.g. 'Contactar'")
    url: str = Field(description="URL to open (e.g. https://wa.me/...)")


class Message(BaseModel):
    """A single outbound message within a multi-message response."""

    text: str = Field(description="Message body text")
    action: MessageAction | None = Field(
        default=None, description="Optional interactive action attached to this message"
    )


class AgentResponse(BaseModel):
    """Structured output returned by the router agent on every turn."""

    intent: Intent = Field(description="Detected user intent")
    message: str = Field(description="Primary response message to send to the user (Spanish)")
    messages: list[Message] = Field(
        default_factory=list,
        description="Additional messages to send individually (e.g. one per provider)",
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Confidence in intent classification"
    )
    entities: dict[str, Any] | None = Field(
        default=None, description="Extracted entities (rubro, zona, nombre, etc.)"
    )
    requires_action: bool = Field(
        default=False, description="True if a business tool was or must be invoked"
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description="Extra data for downstream handlers"
    )