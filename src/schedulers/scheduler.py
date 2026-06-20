"""Central async scheduler engine backed by APScheduler AsyncIO."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop


logger = logging.getLogger(__name__)


class SchedulerEngine:
    """Wraps an AsyncIOScheduler and exposes a minimal async interface."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._started = False

    def register_tasks(self) -> None:
        """Register all enabled jobs from the global task registry."""
        from src.schedulers.registry import registry

        for definition in registry.enabled():
            self._add_job(definition)

    def _add_job(self, definition) -> None:
        kwargs: dict = {
            "id": definition.name,
            "name": definition.name,
            "func": definition.task.execute,
            "max_instances": definition.max_instances,
            "replace_existing": True,
            **definition.kwargs,
        }

        if definition.interval_seconds is not None:
            self._scheduler.add_job(**kwargs, trigger="interval", seconds=definition.interval_seconds)
        elif definition.cron is not None:
            self._scheduler.add_job(**kwargs, trigger="cron", **self._parse_cron(definition.cron))
        else:
            logger.warning(
                "Skipping task %s: neither interval nor cron provided", definition.name
            )

    @staticmethod
    def _parse_cron(cron: str) -> dict[str, str]:
        """Minimal cron parser using APScheduler's native crontab format.

        Args:
            cron: Space-separated cron expression: minute hour day month day_of_week.

        Returns:
            Mapping of crontab kwargs.
        """
        parts = cron.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Invalid cron expression '{cron}': expected 5 parts "
                "(minute hour day month day_of_week)"
            )
        minute, hour, day, month, day_of_week = parts
        return {
            "minute": minute,
            "hour": hour,
            "day": day,
            "month": month,
            "day_of_week": day_of_week,
        }

    async def start(self, loop: AbstractEventLoop | None = None) -> None:
        """Start the scheduler and all registered jobs."""
        if self._started:
            return
        self._scheduler.configure(loop_impl=loop)
        self.register_tasks()
        self._scheduler.start()
        self._started = True
        logger.info(
            "Scheduler started | job_count=%d",
            len(self._scheduler.get_jobs()),
        )

    async def shutdown(self, wait: bool = True) -> None:
        """Stop the scheduler gracefully."""
        if not self._started:
            return
        self._scheduler.shutdown(wait=wait)
        self._started = False
        logger.info("Scheduler shut down")


# Module-level engine singleton
engine = SchedulerEngine()


def iniciar_scheduler() -> AsyncIOScheduler:
    """Create and start the APScheduler instance for scheduled tasks."""
    from src.schedulers.registry import TaskDefinition, registry
    from src.schedulers.tasks.example_task import ExampleTask

    tz_ba = "America/Argentina/Buenos_Aires"

    registry.register(
        TaskDefinition(
            name="tarea_diaria",
            task=ExampleTask(),
            cron="0 9 * * *",
            timezone=tz_ba,
            enabled=True,
        )
    )
    registry.register(
        TaskDefinition(
            name="resumen_semanal",
            task=ExampleTask(),
            cron="0 22 * * 0",
            timezone=tz_ba,
            enabled=True,
        )
    )
    registry.register(
        TaskDefinition(
            name="avanzar_cuotas",
            task=ExampleTask(),
            cron="0 8 1 * *",
            timezone=tz_ba,
            enabled=True,
        )
    )
    registry.register(
        TaskDefinition(
            name="recordatorios",
            task=ExampleTask(),
            cron="0 10 * * *",
            timezone=tz_ba,
            enabled=True,
            misfire_grace_time=3600,
        )
    )
    registry.register(
        TaskDefinition(
            name="recordatorio_nocturno",
            task=ExampleTask(),
            cron="0 20 * * *",
            timezone=tz_ba,
            enabled=True,
        )
    )
    registry.register(
        TaskDefinition(
            name="cleanup_messages_log",
            task=ExampleTask(),
            cron="0 3 * * *",
            timezone=tz_ba,
            enabled=True,
            misfire_grace_time=3600,
        )
    )
    registry.register(
        TaskDefinition(
            name="cleanup_stale_estados",
            task=ExampleTask(),
            interval_seconds=7200,
            enabled=True,
            misfire_grace_time=3600,
        )
    )

    scheduler = AsyncIOScheduler()
    for definition in registry.enabled():
        kwargs = {
            "id": definition.name,
            "name": definition.name,
            "func": definition.task.execute,
            "max_instances": definition.max_instances,
            "replace_existing": True,
            "misfire_grace_time": definition.misfire_grace_time,
            **definition.kwargs,
        }

        if definition.interval_seconds is not None:
            scheduler.add_job(
                **kwargs,
                trigger=IntervalTrigger(seconds=definition.interval_seconds),
            )
        elif definition.cron is not None:
            scheduler.add_job(
                **kwargs,
                trigger=CronTrigger(
                    **SchedulerEngine._parse_cron(definition.cron),
                    timezone=definition.timezone or tz_ba,
                ),
            )

    scheduler.start()
    logger.info(
        "APScheduler iniciado — tarea diaria: 09:00 AR, "
        "recordatorios: 10:00 AR, "
        "resumen semanal: Domingo 22:00 AR, "
        "cuotas: 1ro de cada mes 08:00 AR, "
        "recordatorio nocturno: 20:00 AR diario, "
        "cleanup messages: 03:00 AR diario, "
        "cleanup estados: cada 2h"
    )
    return scheduler
