"""In-memory rate limiter with per-user asyncio locks for thread safety."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from src.infrastructure.config import get_settings

_requests: dict[str, list[float]] = defaultdict(list)
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def check_rate_limit(telefono: str) -> bool:
    """Return True if the request is within the allowed rate.

    Uses a per-user asyncio.Lock so concurrent requests for the same user
    are serialized, preventing list corruption in the shared _requests dict.
    """
    settings = get_settings()
    now = time.time()
    window_start = now - settings.rate_window
    lock = _locks[telefono]

    async with lock:
        calls = [t for t in _requests[telefono] if t > window_start]
        if len(calls) >= settings.rate_limit:
            return False
        calls.append(now)
        _requests[telefono] = calls
        return True
