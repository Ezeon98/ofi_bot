"""Registry for scheduler tasks.

Provides discoverability and metadata for jobs, but does not start anything
on its own. Real management lives in scheduler.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from src.schedulers.tasks.base import BaseTask


@dataclass
class TaskDefinition:
    """Describes a schedulable async task."""

    name: str
    task: BaseTask
    cron: str | None = None
    interval_seconds: int | None = None
    enabled: bool = True
    max_instances: int = 1
    timezone: str | None = None
    misfire_grace_time: int = 60
    kwargs: dict[str, object] = field(default_factory=dict)


class TaskRegistry:
    """Central catalogue of registered background tasks."""

    def __init__(self) -> None:
        self._registry: dict[str, TaskDefinition] = {}

    def register(self, definition: TaskDefinition) -> None:
        """Add or replace a task definition by name."""
        self._registry[definition.name] = definition

    def get(self, name: str) -> TaskDefinition | None:
        """Lookup a registered task by name."""
        return self._registry.get(name)

    def all(self) -> list[TaskDefinition]:
        """Return all registered task definitions."""
        return list(self._registry.values())

    def enabled(self) -> list[TaskDefinition]:
        """Return only enabled tasks."""
        return [t for t in self._registry.values() if t.enabled]


# Global registry instance
registry = TaskRegistry()