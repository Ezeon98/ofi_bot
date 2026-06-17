"""In-memory rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict

from src.infrastructure.config import get_settings

_requests: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(telefono: str) -> bool:
    """Return True if the request is within the allowed rate."""
    settings = get_settings()
    now = time.time()
    window_start = now - settings.rate_window
    calls = [t for t in _requests[telefono] if t > window_start]
    if len(calls) >= settings.rate_limit:
        return False
    calls.append(now)
    _requests[telefono] = calls
    return True
