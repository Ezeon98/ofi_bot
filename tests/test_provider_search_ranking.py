"""Focused tests for provider search ranking with geographic metadata."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch


def _load_providers_module():
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object

    with patch.dict(sys.modules, {"pydantic_ai": fake_pydantic_ai}):
        return import_module("src.tools.business.providers")


providers = _load_providers_module()


class ProviderSearchRankingTests(IsolatedAsyncioTestCase):
    """Validate ranking behavior when origin coordinates are available."""

    def test_legacy_rubro_json_pattern_matches_exact_array_item(self) -> None:
        """Legacy JSON rubros should be matched by exact element, not fuzzy stems."""
        pattern = providers._legacy_rubro_json_pattern("Niñera")

        self.assertEqual(pattern, '%"Niñera"%')

    def test_search_params_clamp_limit_to_three(self) -> None:
        """Searches should never return more than three provider cards."""
        params = providers.BuscarPrestadoresInput(
            rubro="plomero",
            barrio="Castelar",
            ciudad="Castelar",
            limit=5,
        )

        sanitized = providers._sanitize_search_params(params)

        self.assertEqual(sanitized.limit, 3)

    def test_search_params_strip_trade_text_from_barrio(self) -> None:
        """Barrio inputs contaminated with the rubro should keep only the zone."""
        params = providers.BuscarPrestadoresInput(
            rubro="electricistas",
            barrio="Electricista en Caballito",
            limit=3,
        )

        sanitized = providers._sanitize_search_params(params)

        self.assertEqual(sanitized.barrio, "Caballito")
        self.assertIsNone(sanitized.ciudad)

    async def test_rubro_search_uses_exact_trade_filters_only(self) -> None:
        """Searching should filter only against the provider rubros JSON field."""
        captured_sql: dict[str, str] = {}

        async def execute_side_effect(stmt):
            captured_sql["sql"] = str(
                stmt.compile(compile_kwargs={"literal_binds": True})
            )
            return []

        fake_db = SimpleNamespace(execute=AsyncMock(side_effect=execute_side_effect))
        ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db=fake_db,
                current_message_metadata={"latitude": -34.6037, "longitude": -58.3816},
            )
        )

        await providers.buscar_prestadores(
            ctx,
            providers.BuscarPrestadoresInput(rubro="Niñera", barrio="Palermo", limit=3),
        )

        sql = captured_sql["sql"]
        self.assertIn("providers.rubros LIKE '%\"Niñera\"%'", sql)
        self.assertNotIn("trades", sql.lower())
        self.assertNotIn("%n%", sql)
        self.assertNotIn("%ninera%", sql)

    async def test_busqueda_uses_message_metadata_for_distance_ranking(self) -> None:
        """Metadata coordinates should be enough to compute and expose result distances."""
        verified_far = SimpleNamespace(
            id=1,
            nombre="Proveedor Verificado",
            rubros='["Plomeria"]',
            ciudad="La Plata",
            barrio="Centro",
            lat=-34.9205,
            lon=-57.9536,
            disponibilidad="Lunes a viernes",
            badge_activo=True,
            facturacion="monotributo",
        )
        verified_near = SimpleNamespace(
            id=2,
            nombre="Proveedor Cercano",
            rubros='["Plomeria"]',
            ciudad="CABA",
            barrio="Caballito",
            lat=-34.6183,
            lon=-58.4432,
            disponibilidad="Guardias",
            badge_activo=True,
            facturacion="monotributo",
        )
        unverified_near = SimpleNamespace(
            id=3,
            nombre="Proveedor Base",
            rubros='["Plomeria"]',
            ciudad="CABA",
            barrio="Almagro",
            lat=-34.6100,
            lon=-58.4200,
            disponibilidad="Sujeto a agenda",
            badge_activo=False,
            facturacion="monotributo",
        )

        fake_db = SimpleNamespace(
            execute=AsyncMock(
                return_value=[
                    (verified_far, "5491100001001"),
                    (verified_near, "5491100001002"),
                    (unverified_near, "5491100001003"),
                ]
            )
        )
        ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db=fake_db,
                current_message_metadata={"latitude": -34.6037, "longitude": -58.3816},
            )
        )

        results = await providers.buscar_prestadores(
            ctx,
            providers.BuscarPrestadoresInput(rubro="plomero", limit=3),
        )

        self.assertEqual(results[0]["nombre"], "Proveedor Cercano")
        self.assertEqual(results[1]["nombre"], "Proveedor Verificado")
        self.assertEqual(results[2]["nombre"], "Proveedor Base")
        self.assertIsNotNone(results[0]["distance_km"])

    async def test_textual_zone_is_geocoded_for_distance_ranking(self) -> None:
        """A plain-text zone like Wilde should resolve to coordinates for ranking."""
        far_verified = SimpleNamespace(
            id=1,
            nombre="Proveedor Lejano",
            rubros='["Plomeria"]',
            ciudad="La Plata",
            barrio="Centro",
            lat=-34.9205,
            lon=-57.9536,
            disponibilidad="Lunes a viernes",
            badge_activo=True,
            facturacion="monotributo",
        )
        near_verified = SimpleNamespace(
            id=2,
            nombre="Proveedor Wilde",
            rubros='["Plomeria"]',
            ciudad="Avellaneda",
            barrio="Wilde",
            lat=-34.7042,
            lon=-58.3211,
            disponibilidad="Guardias",
            badge_activo=True,
            facturacion="monotributo",
        )

        fake_db = SimpleNamespace(
            execute=AsyncMock(
                return_value=[
                    (far_verified, "5491100001001"),
                    (near_verified, "5491100001002"),
                ]
            )
        )
        ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db=fake_db,
                current_message_metadata=None,
            )
        )

        with patch.object(
                providers,
                "geocode_text_location",
                new=AsyncMock(return_value={"lat": -34.7060, "lon": -58.3190}),
            ) as geocode_mock:
            results = await providers.buscar_prestadores(
                ctx,
                providers.BuscarPrestadoresInput(rubro="plomero", barrio="Wilde", limit=3),
            )

        geocode_mock.assert_awaited_once_with("Wilde")
        self.assertEqual(results[0]["nombre"], "Proveedor Wilde")
        self.assertIsNotNone(results[0]["distance_km"])

    async def test_resolve_search_origin_prefers_explicit_params_over_metadata(self) -> None:
        """Tool inputs should override message metadata when both are present."""
        origin = await providers._resolve_search_origin(
            {"latitude": -34.6, "longitude": -58.4},
            providers.BuscarPrestadoresInput(
                rubro="plomero",
                lat=-34.1,
                lon=-58.1,
                limit=3,
            ),
        )

        self.assertEqual(origin, (-34.1, -58.1))

    def test_text_location_filter_is_disabled_when_origin_has_coordinates(self) -> None:
        """Once the search origin is known, textual filtering should not narrow results."""
        self.assertFalse(providers._should_apply_text_location_filter("Wilde", "Avellaneda", -34.70, -58.32))
        self.assertTrue(providers._should_apply_text_location_filter("Wilde", None, None, None))