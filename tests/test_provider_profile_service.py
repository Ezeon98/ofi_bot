"""Focused tests for provider-profile shortcuts."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.agents.models.response import Intent


def _load_provider_profile_service_module():
    """Import the service with the minimal pydantic_ai stub required."""
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object

    with patch.dict(sys.modules, {"pydantic_ai": fake_pydantic_ai}):
        return import_module("src.application.services.provider_profile_service")


provider_profile_service = _load_provider_profile_service_module()
ProviderProfileService = provider_profile_service.ProviderProfileService


class ProviderProfileServiceTests(IsolatedAsyncioTestCase):
    """Validate the explicit provider-profile shortcuts."""

    def _deps(self, db) -> SimpleNamespace:
        """Build the minimal deps object used by the service."""
        return SimpleNamespace(
            db=db,
            user_id="5491162527111",
            usuario_id=71,
        )

    async def test_add_trades_merges_new_rubros_into_profile(self) -> None:
        """Adding provider rubros should preserve existing ones and append new ones."""
        service = ProviderProfileService()
        db = SimpleNamespace(execute=AsyncMock())
        provider_row = SimpleNamespace(id=9, rubros='["Electricidad"]')

        with patch.object(
            ProviderProfileService,
            "_load_provider",
            new=AsyncMock(return_value=provider_row),
        ):
            response = await service.maybe_handle_profile_update(
                user_id="5491162527111",
                message="Tambien hago plomeria y gas",
                metadata={"message_type": "text"},
                deps=self._deps(db),
                turn_id="profile-rubros",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.ACTUALIZAR_PERFIL)
        self.assertEqual(
            response.entities["rubros"],
            ["Electricidad", "Plomeria", "Gas"],
        )
        self.assertEqual(response.entities["rubros_agregados"], ["Plomeria", "Gas"])
        db.execute.assert_awaited_once()

    async def test_location_update_updates_provider_coordinates(self) -> None:
        """A relocation announcement in profile mode should update provider location."""
        service = ProviderProfileService()
        db = SimpleNamespace(execute=AsyncMock())
        provider_row = SimpleNamespace(id=9, rubros='["Electricidad"]')

        with (
            patch.object(
                ProviderProfileService,
                "_load_provider",
                new=AsyncMock(return_value=provider_row),
            ),
            patch.object(
                provider_profile_service.ProviderSearchService,
                "_enrich_location_with_coords",
                new=AsyncMock(
                    return_value={
                        "barrio": "avellaneda",
                        "ciudad": "Avellaneda",
                        "lat": -34.66,
                        "lon": -58.37,
                    }
                ),
            ),
        ):
            response = await service.maybe_handle_profile_update(
                user_id="5491162527111",
                message="Me mude a Avellaneda",
                metadata={"message_type": "text"},
                deps=self._deps(db),
                turn_id="profile-location",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.ACTUALIZAR_PERFIL)
        self.assertEqual(response.entities["barrio"], "avellaneda")
        self.assertEqual(response.entities["ciudad"], "Avellaneda")
        db.execute.assert_awaited_once()
        stmt = db.execute.await_args.args[0]
        params = stmt.compile().params
        self.assertEqual(params["barrio"], "avellaneda")
        self.assertEqual(params["ciudad"], "Avellaneda")
        self.assertEqual(params["lat"], -34.66)
        self.assertEqual(params["lon"], -58.37)

    async def test_profile_update_without_provider_returns_helpful_message(self) -> None:
        """Profile updates should explain that registration is still missing."""
        service = ProviderProfileService()
        db = SimpleNamespace(execute=AsyncMock())

        with patch.object(
            ProviderProfileService,
            "_load_provider",
            new=AsyncMock(return_value=None),
        ):
            response = await service.maybe_handle_profile_update(
                user_id="5491162527111",
                message="Tambien hago plomeria",
                metadata={"message_type": "text"},
                deps=self._deps(db),
                turn_id="profile-no-provider",
            )

        self.assertIsNotNone(response)
        self.assertEqual(response.intent, Intent.ACTUALIZAR_PERFIL)
        self.assertIn("Todavía no tenés un perfil", response.message)
        db.execute.assert_not_awaited()
