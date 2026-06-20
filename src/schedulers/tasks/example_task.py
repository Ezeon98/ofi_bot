"""Placeholder async task used for scheduler integration verification."""

from __future__ import annotations

import logging
from typing import Any

from src.schedulers.tasks.base import BaseTask

logger = logging.getLogger(__name__)


class ExampleTask(BaseTask):
    """No-op placeholder task.

    Replace or extend concrete tasks in this package as needed.
    """

    async def execute(self, **kwargs: Any) -> None:
        logger.info("ExampleTask executed — no-op placeholder | kwargs=%s", kwargs)