"""Helpers for building OpenAI clients with API-key failover."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from openai import (
    APIStatusError,
    AsyncOpenAI,
    AuthenticationError,
    PermissionDeniedError,
)

from src.infrastructure.config import Settings

logger = logging.getLogger(__name__)


def _is_quota_failure(exc: Exception) -> bool:
    """Return True when the exception indicates exhausted quota/billing."""
    if not isinstance(exc, APIStatusError) or exc.status_code != 429:
        return False

    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False

    code = error.get("code")
    error_type = error.get("type")
    return code == "insufficient_quota" or error_type == "insufficient_quota"


def _is_auth_failure(exc: Exception) -> bool:
    """Return True when the exception indicates rejected credentials or quota."""
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in {401, 403}:
        return True
    return _is_quota_failure(exc)


class _OpenAIResourceProxy:
    """Proxy nested OpenAI resources back to the failover client."""

    def __init__(
        self,
        client: OpenAIClientWithFallback,
        path: tuple[str, ...],
    ) -> None:
        self._client = client
        self._path = path

    def __getattr__(self, name: str) -> _OpenAIResourceProxy:
        """Extend the resource path lazily."""
        return _OpenAIResourceProxy(self._client, self._path + (name,))

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        """Retry create calls with the secondary key on auth/quota failures."""
        return await self._client.call_with_failover(
            self._path + ("create",),
            *args,
            **kwargs,
        )


class OpenAIClientWithFallback:
    """Proxy AsyncOpenAI calls across primary and secondary API keys."""

    def __init__(self, api_keys: Sequence[str]) -> None:
        normalized_keys: list[str] = []
        for key in api_keys:
            stripped = key.strip()
            if stripped and stripped not in normalized_keys:
                normalized_keys.append(stripped)
        if not normalized_keys:
            normalized_keys.append("")

        self._clients = [AsyncOpenAI(api_key=key) for key in normalized_keys]
        self._active_index = 0

    def __getattr__(self, name: str) -> _OpenAIResourceProxy:
        """Start a new nested resource path from the OpenAI root client."""
        return _OpenAIResourceProxy(self, (name,))

    async def call_with_failover(
        self,
        path: tuple[str, ...],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute one OpenAI request, retrying with the next key on auth/quota errors."""
        client_indexes = [
            self._active_index,
            *[index for index in range(len(self._clients)) if index != self._active_index],
        ]
        last_exc: Exception | None = None

        for position, client_index in enumerate(client_indexes):
            try:
                result = await self._invoke(client_index, path, *args, **kwargs)
                if self._active_index != client_index:
                    logger.warning("OpenAI API key fallback activated; using secondary key")
                    self._active_index = client_index
                return result
            except Exception as exc:
                if not _is_auth_failure(exc):
                    raise
                last_exc = exc
                if position == len(client_indexes) - 1:
                    raise
                logger.warning("OpenAI API key failed auth/quota check; trying next configured key")

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenAI client failover exhausted without a result")

    async def _invoke(
        self,
        client_index: int,
        path: tuple[str, ...],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Resolve the nested OpenAI method and execute it."""
        resource: Any = self._clients[client_index]
        for segment in path:
            resource = getattr(resource, segment)
        return await resource(*args, **kwargs)


def build_openai_client(settings: Settings) -> OpenAIClientWithFallback:
    """Build the shared OpenAI client with primary and secondary API keys."""
    return OpenAIClientWithFallback(settings.openai_api_keys())
