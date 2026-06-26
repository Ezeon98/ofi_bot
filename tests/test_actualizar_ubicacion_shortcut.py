"""Tests for the location-update shortcut in ProviderSearchService.

Covers:
 - _extract_location_update: phrase matching and edge cases
 - maybe_handle_location_update: correct response, memory persistence, state clear
"""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.agents.models.response import Intent


def _load_service():
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object
    with patch.dict(sys.modules, {"pydantic_ai": fake_pydantic_ai}):
        return import_module("src.application.services.provider_search_service")


pss = _load_service()
ProviderSearchService = pss.ProviderSearchService


# ── _extract_location_update ──────────────────────────────────────────────────


class ExtractLocationUpdateTests(IsolatedAsyncioTestCase):

    def _extract(self, msg: str):
        return ProviderSearchService._extract_location_update(msg)

    def test_me_mude_a(self):
        self.assertEqual(self._extract("Me mude a Avellaneda"), "avellaneda")

    def test_me_mudé_a_with_accent(self):
        self.assertEqual(self._extract("Me mudé a Palermo"), "palermo")

    def test_me_mude_para(self):
        self.assertEqual(self._extract("me mude para quilmes"), "quilmes")

    def test_ahora_vivo_en(self):
        self.assertEqual(self._extract("Ahora vivo en La Plata"), "la plata")

    def test_estoy_viviendo_en(self):
        self.assertEqual(self._extract("estoy viviendo en Belgrano"), "belgrano")

    def test_cambie_de_casa_a(self):
        self.assertEqual(self._extract("Cambie de casa a Morón"), "morón")

    def test_me_cambie_a(self):
        self.assertEqual(self._extract("me cambie a Lomas de Zamora"), "lomas de zamora")

    def test_no_match_plain_message(self):
        self.assertIsNone(self._extract("Hola, ¿cómo estás?"))

    def test_no_match_search_message(self):
        self.assertIsNone(self._extract("Buscame un electricista en Caballito"))

    def test_no_match_empty_zone(self):
        # prefix present but nothing after it
        self.assertIsNone(self._extract("me mude a"))

    def test_no_match_zone_too_long(self):
        # zone fragment > 6 words should be rejected
        self.assertIsNone(
            self._extract("me mude a un lugar muy lejano del otro lado del pais")
        )

    def test_no_match_zone_contains_search_prefix(self):
        # "buscame un plomero" starts a search, not a zone
        self.assertIsNone(self._extract("me mude a buscame un plomero"))


# ── maybe_handle_location_update ─────────────────────────────────────────────


class MaybeHandleLocationUpdateTests(IsolatedAsyncioTestCase):

    def _make_service(self):
        return ProviderSearchService(memory_config=SimpleNamespace(enabled=True))

    def _make_deps(self, db=None):
        return SimpleNamespace(
            db=db or object(),
            usuario_id=42,
            user_id="5491162527111",
        )

    async def test_returns_none_for_non_location_message(self):
        service = self._make_service()
        result = await service.maybe_handle_location_update(
            user_id="5491162527111",
            message="Buscame un plomero",
            deps=self._make_deps(),
            memory_service=SimpleNamespace(),
            metadata=None,
        )
        self.assertIsNone(result)

    async def test_returns_actualizar_ubicacion_intent(self):
        service = self._make_service()
        memory_service = SimpleNamespace(upsert_memory=AsyncMock())
        fake_db = object()
        fake_state_repo = SimpleNamespace(delete=AsyncMock())

        with (
            patch.object(
                pss,
                "EstadoRepository",
                return_value=fake_state_repo,
            ),
            patch.object(
                ProviderSearchService,
                "_enrich_location_with_coords",
                new=AsyncMock(return_value={"barrio": "avellaneda", "ciudad": "Avellaneda", "lat": -34.66, "lon": -58.37}),
            ),
        ):
            result = await service.maybe_handle_location_update(
                user_id="5491162527111",
                message="Me mude a Avellaneda",
                deps=self._make_deps(db=fake_db),
                memory_service=memory_service,
                metadata=None,
                turn_id="t1",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.intent, Intent.ACTUALIZAR_UBICACION)
        self.assertFalse(result.requires_action)
        self.assertIn("Avellaneda", result.message)

    async def test_persists_location_to_memory(self):
        service = self._make_service()
        memory_service = SimpleNamespace(upsert_memory=AsyncMock())
        fake_state_repo = SimpleNamespace(delete=AsyncMock())

        with (
            patch.object(pss, "EstadoRepository", return_value=fake_state_repo),
            patch.object(
                ProviderSearchService,
                "_enrich_location_with_coords",
                new=AsyncMock(return_value={"barrio": "palermo", "ciudad": None, "lat": None, "lon": None}),
            ),
        ):
            await service.maybe_handle_location_update(
                user_id="5491162527111",
                message="Me mude a Palermo",
                deps=self._make_deps(),
                memory_service=memory_service,
                metadata=None,
            )

        # barrio should have been persisted
        calls = [call.args[1] for call in memory_service.upsert_memory.await_args_list]
        self.assertIn("barrio", calls)

    async def test_clears_active_search_state(self):
        service = self._make_service()
        memory_service = SimpleNamespace(upsert_memory=AsyncMock())
        fake_state_repo = SimpleNamespace(delete=AsyncMock())

        with (
            patch.object(pss, "EstadoRepository", return_value=fake_state_repo),
            patch.object(
                ProviderSearchService,
                "_enrich_location_with_coords",
                new=AsyncMock(return_value={"barrio": "quilmes", "ciudad": None, "lat": None, "lon": None}),
            ),
        ):
            await service.maybe_handle_location_update(
                user_id="5491162527111",
                message="me mude a Quilmes",
                deps=self._make_deps(),
                memory_service=memory_service,
                metadata=None,
            )

        fake_state_repo.delete.assert_awaited_once_with("5491162527111")
