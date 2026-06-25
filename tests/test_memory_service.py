"""Focused regression tests for MemoryService session serialization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from src.memory.models import MemoryConfig
from src.memory.service import MemoryService


class _BlockingMemoryRepository:
    """Simulate a repository call that would overlap without a service lock."""

    def __init__(self) -> None:
        self.active_calls = 0
        self.max_active_calls = 0
        self.first_call_started = asyncio.Event()
        self.allow_first_call_to_finish = asyncio.Event()
        self.call_count = 0

    async def upsert(self, user_id: int, data) -> dict[str, object]:
        self.call_count += 1
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)

        if self.call_count == 1:
            self.first_call_started.set()
            await self.allow_first_call_to_finish.wait()

        await asyncio.sleep(0)
        self.active_calls -= 1
        return {"user_id": user_id, "key": data.key, "value": data.value}


class MemoryServiceTests(IsolatedAsyncioTestCase):
    """Guard request-scoped AsyncSession access inside MemoryService."""

    async def test_upsert_memory_serializes_concurrent_repository_writes(self) -> None:
        """Concurrent memory writes should not enter the repository at the same time."""
        service = MemoryService(
            session=SimpleNamespace(),
            extractor=SimpleNamespace(extract=None),
            summarizer=SimpleNamespace(summarize=None),
            config=MemoryConfig(),
        )
        fake_repo = _BlockingMemoryRepository()
        service._memories = fake_repo

        first_task = asyncio.create_task(
            service.upsert_memory(71, "ciudad", "Avellaneda", 0.9)
        )
        await fake_repo.first_call_started.wait()
        second_task = asyncio.create_task(
            service.upsert_memory(71, "barrio", "Piñeyro", 0.8)
        )
        await asyncio.sleep(0)
        fake_repo.allow_first_call_to_finish.set()

        await asyncio.gather(first_task, second_task)

        self.assertEqual(fake_repo.max_active_calls, 1)