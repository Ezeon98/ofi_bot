"""AgentLogger — structured logging for the AI agent pipeline.

Each message from a user generates a single **turn** with a unique
``turn_id`` (correlation ID).  Every log line emitted during that turn
includes the turn_id so you can trace the full agent decision path.

Usage::

    from src.utils.agent_logger import AgentLogger

    alog = AgentLogger(settings.agent_logging_enabled)
    alog.info(turn_id, "step_name", key="value")

To enable/disable::

    AGENT_LOGGING_ENABLED=false  # default: true
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger("agent")


class AgentLogger:
    """Structured turn-level logger for the AI pipeline.

    Every call is a no-op when ``enabled`` is ``False``, so it's safe to
    leave the instrumentation in production and toggle via an env-var.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def new_turn_id(self) -> str:
        """Return a fresh correlation ID for the current user message."""
        return uuid.uuid4().hex[:12]

    def info(
        self,
        turn_id: str,
        step: str,
        **fields: Any,
    ) -> None:
        """Emit a structured log line for the given turn + step.

        Parameters
        ----------
        turn_id:
            Correlation ID returned by :meth:`new_turn_id`.
        step:
            Short name of the pipeline stage, e.g. ``"router.in"``,
            ``"provider_search"``, ``"memory_get"``.
        **fields:
            Arbitrary key-value data to attach to the log line.
        """
        if not self._enabled:
            return
        record: dict[str, Any] = {
            "turn_id": turn_id,
            "step": step,
            **fields,
        }
        logger.info(json.dumps(record, default=str))

    def warn(
        self,
        turn_id: str,
        step: str,
        **fields: Any,
    ) -> None:
        """Emit a structured warning for the given turn + step."""
        if not self._enabled:
            return
        record: dict[str, Any] = {
            "turn_id": turn_id,
            "step": step,
            **fields,
        }
        logger.warning(json.dumps(record, default=str))

    def error(
        self,
        turn_id: str,
        step: str,
        **fields: Any,
    ) -> None:
        """Emit a structured error for the given turn + step."""
        if not self._enabled:
            return
        record: dict[str, Any] = {
            "turn_id": turn_id,
            "step": step,
            **fields,
        }
        logger.error(json.dumps(record, default=str))
