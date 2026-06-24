"""Focused tests for provider business tools."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock


def _load_provider_tools_module():
    """Import provider tools with the minimal pydantic_ai stub required."""
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object
    sys.modules.setdefault("pydantic_ai", fake_pydantic_ai)
    return import_module("src.tools.business.providers")


provider_tools = _load_provider_tools_module()


class _FakeDb:
    """Tiny fake async DB used to verify provider persistence wiring."""

    def __init__(self) -> None:
        self.scalar = AsyncMock(side_effect=[42, None])
        self.flush = AsyncMock()
        self.added = None

    def add(self, obj) -> None:
        self.added = obj
        obj.id = 99


class ProviderToolsTests(IsolatedAsyncioTestCase):
    """Lock down phone-to-user-id resolution for provider creation."""

    async def test_crear_prestador_resolves_phone_to_usuario_id(self) -> None:
        """Creating a provider should link the row to the DB user, not the phone."""
        db = _FakeDb()
        ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db=db,
                user_id="5491162527111",
            )
        )
        params = provider_tools.CrearPrestadorInput(
            nombre="Juan Perez",
            rubros=["Plomeria", "Electricidad"],
            barrio="Caballito",
            ciudad="CABA",
        )

        result = await provider_tools.crear_prestador(ctx, params)

        self.assertEqual(result["id"], 99)
        self.assertIsNotNone(db.added)
        self.assertEqual(db.added.usuario_id, 42)
        self.assertEqual(db.added.nombre, "Juan Perez")