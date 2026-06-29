"""Focused tests for provider business tools."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch


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

    async def test_busqueda_rubros_relacionados_devuelve_alternativas(self) -> None:
        """The AI helper should expose close canonical rubro alternatives."""
        result = await provider_tools.buscar_rubros_relacionados(
            SimpleNamespace(),
            provider_tools.RubrosRelacionadosInput(
                rubro="Electricista industrial",
                limit=4,
            ),
        )

        self.assertEqual(result["rubro"], "Electricista industrial")
        self.assertIn("Electricista domiciliario", result["alternativas"])
        self.assertNotIn("Electricista industrial", result["alternativas"])

    async def test_resolver_ubicacion_devuelve_geocoding_estructurado(self) -> None:
        """The AI helper should normalize a textual zone into location fields."""
        with patch.object(
            provider_tools,
            "geocode_text_location",
            new=AsyncMock(
                return_value={
                    "barrio": None,
                    "ciudad": "Lanús",
                    "lat": -34.699,
                    "lon": -58.392,
                    "display_name": "Lanús, Buenos Aires, Argentina",
                }
            ),
        ) as geocode_mock:
            result = await provider_tools.resolver_ubicacion(
                SimpleNamespace(),
                provider_tools.ResolverUbicacionInput(ubicacion="Lanus"),
            )

        geocode_mock.assert_awaited_once_with("Lanus")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["ciudad"], "Lanús")
        self.assertEqual(result["query"], "Lanus")