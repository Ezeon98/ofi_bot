"""Tests for OpenAI API-key failover behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from src.infrastructure.external import openai_client


def _build_fake_client(create_side_effect):
    """Return a minimal AsyncOpenAI-shaped object for failover tests."""
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_side_effect)
        )
    )


class _DummyQuotaError(Exception):
    """Small test-only quota error shaped like an OpenAI API status failure."""

    def __init__(self) -> None:
        self.status_code = 429
        self.body = {
            "error": {
                "code": "insufficient_quota",
                "type": "insufficient_quota",
            }
        }
        super().__init__("insufficient quota")


class OpenAIFallbackTests(IsolatedAsyncioTestCase):
    """Validate retry behavior across primary and secondary API keys."""

    async def test_retries_with_secondary_key_after_auth_failure(self) -> None:
        """A rejected primary key should transparently fall back to the secondary."""
        class DummyAuthError(Exception):
            """Small test-only auth error used to drive failover."""

        primary_error = DummyAuthError("invalid api key")

        async def primary_create(*_args, **_kwargs):
            raise primary_error

        async def secondary_create(*_args, **_kwargs):
            return {"ok": True}

        fake_clients = [
            _build_fake_client(primary_create),
            _build_fake_client(secondary_create),
        ]

        with patch.object(
            openai_client,
            "AsyncOpenAI",
            side_effect=fake_clients,
        ), patch.object(
            openai_client,
            "_is_auth_failure",
            side_effect=lambda exc: isinstance(exc, DummyAuthError),
        ):
            client = openai_client.OpenAIClientWithFallback(
                ["primary-key", "secondary-key"]
            )
            result = await client.chat.completions.create(model="gpt-4o-mini")

        self.assertEqual(result, {"ok": True})

    async def test_non_auth_errors_do_not_retry_other_keys(self) -> None:
        """Only authentication failures should trigger the secondary key."""

        async def failing_create(*_args, **_kwargs):
            raise RuntimeError("boom")

        fake_clients = [
            _build_fake_client(failing_create),
            _build_fake_client(failing_create),
        ]

        with patch.object(
            openai_client,
            "AsyncOpenAI",
            side_effect=fake_clients,
        ):
            client = openai_client.OpenAIClientWithFallback(
                ["primary-key", "secondary-key"]
            )
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await client.chat.completions.create(model="gpt-4o-mini")

    async def test_retries_with_secondary_key_after_quota_failure(self) -> None:
        """Insufficient quota on the primary key should fall back to the secondary."""

        async def primary_create(*_args, **_kwargs):
            raise _DummyQuotaError()

        async def secondary_create(*_args, **_kwargs):
            return {"ok": "secondary"}

        fake_clients = [
            _build_fake_client(primary_create),
            _build_fake_client(secondary_create),
        ]

        with patch.object(
            openai_client,
            "AsyncOpenAI",
            side_effect=fake_clients,
        ), patch.object(
            openai_client,
            "APIStatusError",
            _DummyQuotaError,
        ):
            client = openai_client.OpenAIClientWithFallback(
                ["primary-key", "secondary-key"]
            )
            result = await client.chat.completions.create(model="gpt-4o-mini")

        self.assertEqual(result, {"ok": "secondary"})
