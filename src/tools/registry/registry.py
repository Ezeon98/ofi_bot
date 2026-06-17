"""Tool Registry — decoupled registration of agent tools.

Usage:
    registry = ToolRegistry()
    registry.register(my_tool_function)
    registry.install_into(agent)

Each tool is a regular async function decorated externally. The registry
keeps them as a list so the agent can be rebuilt or hot-reloaded without
coupling to specific module paths.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Holds a collection of tool callables and installs them into a PydanticAI Agent."""

    def __init__(self) -> None:
        self._tools: list[Callable[..., Any]] = []

    def register(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a tool. Can be used as a decorator."""
        self._tools.append(fn)
        logger.debug("Registered tool: %s", fn.__name__)
        return fn

    def install_into(self, agent: Agent) -> None:  # type: ignore[type-arg]
        """Install all registered tools into an agent instance."""
        for fn in self._tools:
            agent.tool(fn)

    @property
    def tools(self) -> list[Callable[..., Any]]:
        return list(self._tools)


# Module-level default registry — import and use from any tool module.
default_registry = ToolRegistry()
