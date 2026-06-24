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

    async def test_rubro_search_expands_profession_terms_for_legacy_labels(self) -> None:
        """Searching 'plomero' should still match a provider stored as 'Plomeria'."""
        palermo_provider = SimpleNamespace(
            id=1,
            nombre="Juan Plomero",
            rubros='["Plomeria"]',
            ciudad="CABA",
            barrio="Palermo",
            lat=-34.5875,
            lon=-58.4201,
            disponibilidad="Lunes a sabado",
            badge_activo=True,
            facturacion="monotributo",
        )

        async def scalars_side_effect(stmt):
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
            if "%plomer%" in sql and "%plomero%" in sql:
                return [palermo_provider]
            return []

        fake_db = SimpleNamespace(scalars=AsyncMock(side_effect=scalars_side_effect))
        ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db=fake_db,
                current_message_metadata={"latitude": -34.6037, "longitude": -58.3816},
            )
        )

        with patch.object(
            providers,
            "_provider_trade_names",
            new=AsyncMock(return_value=["Plomeria"]),
        ):
            results = await providers.buscar_prestadores(
                ctx,
                providers.BuscarPrestadoresInput(rubro="plomero", barrio="Palermo", limit=3),
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["nombre"], "Juan Plomero")

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

        fake_db = SimpleNamespace(scalars=AsyncMock(return_value=[verified_far, verified_near, unverified_near]))
        ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db=fake_db,
                current_message_metadata={"latitude": -34.6037, "longitude": -58.3816},
            )
        )

        with patch.object(
            providers,
            "_provider_trade_names",
            new=AsyncMock(return_value=["Plomeria"]),
        ):
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

        fake_db = SimpleNamespace(scalars=AsyncMock(return_value=[far_verified, near_verified]))
        ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db=fake_db,
                current_message_metadata=None,
            )
        )

        with (
            patch.object(
                providers,
                "_provider_trade_names",
                new=AsyncMock(return_value=["Plomeria"]),
            ),
            patch.object(
                providers,
                "geocode_text_location",
                new=AsyncMock(return_value={"lat": -34.7060, "lon": -58.3190}),
            ) as geocode_mock,
        ):
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