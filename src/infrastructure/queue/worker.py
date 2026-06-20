"""arq worker — processes WhatsApp webhook payloads from a Redis queue."""

from __future__ import annotations

import logging
import os
from typing import Any

from arq import create_pool
from arq.connections import RedisSettings

from src.infrastructure.config import get_settings
from src.infrastructure.queue.processor import process_webhook_entries

logger = logging.getLogger(__name__)
settings = get_settings()


async def process_webhook(ctx: dict[str, Any], body: dict) -> None:
    """Job: process a WhatsApp webhook payload serially via Redis queue."""
    await process_webhook_entries(body)


async def startup(ctx: dict[str, Any]) -> None:
    """Worker startup hook."""
    logger.info("arq worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    """Worker shutdown hook."""
    logger.info("arq worker stopped")


def get_redis_settings() -> RedisSettings:
    """Build arq RedisSettings from environment."""
    return RedisSettings(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
    )


if __name__ == "__main__":
    import asyncio

    async def run() -> None:
        redis_pool = await create_pool(get_redis_settings())
        worker = Worker(
            functions=[process_webhook],
            redis_pool=redis_pool,
            on_startup=startup,
            on_shutdown=shutdown,
        )
        await worker.run()

    from arq.worker import Worker

    asyncio.run(run())