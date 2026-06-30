"""Base abstractions for pluggable async tasks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseTask(ABC):
    """Contract every scheduled job must implement."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> None:
        """Run the task logic. Currently a placeholder."""
