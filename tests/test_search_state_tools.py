"""Focused tests for guided-search conversation state tools."""

from __future__ import annotations

import sys
from importlib import import_module
from types import SimpleNamespace
from types import ModuleType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch


def _load_search_state_module():
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object

    with patch.dict(sys.modules, {"pydantic_ai": fake_pydantic_ai}):
        return import_module("src.tools.business.search_state")


search_state = _load_search_state_module()


def _ctx() -> SimpleNamespace:
    deps = SimpleNamespace(db=object(), user_id="5491112345678")
    return SimpleNamespace(deps=deps)


class SearchStateToolTests(IsolatedAsyncioTestCase):
    """Validate guided-search state reads, writes and cleanup."""

    async def test_consultar_estado_busqueda_returns_inactive_when_missing(self) -> None:
        """The state tool should report no active flow when the repo is empty."""
        repo = SimpleNamespace(get=AsyncMock(return_value={}))

        with patch.object(search_state, "EstadoRepository", return_value=repo):
            result = await search_state.consultar_estado_busqueda(
                _ctx(),
                search_state.ConsultarEstadoBusquedaInput(),
            )

        self.assertEqual(
            result,
            {
                "activo": False,
                "paso": None,
                "rubro": None,
                "zona": None,
                "detalle": None,
            },
        )

    async def test_guardar_estado_busqueda_persists_namespaced_state(self) -> None:
        """Saving search state should namespace it under guided_provider_search."""
        repo = SimpleNamespace(save=AsyncMock())

        with patch.object(search_state, "EstadoRepository", return_value=repo):
            result = await search_state.guardar_estado_busqueda(
                _ctx(),
                search_state.GuardarEstadoBusquedaInput(
                    paso="awaiting_zone",
                    rubro="plomero",
                    detalle="urgente",
                ),
            )

        repo.save.assert_awaited_once_with(
            "5491112345678",
            {
                "estado": search_state.SEARCH_STATE_NAME,
                "paso": "awaiting_zone",
                "rubro": "plomero",
                "zona": None,
                "detalle": "urgente",
            },
        )
        self.assertTrue(result["guardado"])

    async def test_limpiar_estado_busqueda_deletes_repo_state(self) -> None:
        """Cleaning search state should delegate to the estado repository."""
        repo = SimpleNamespace(delete=AsyncMock())

        with patch.object(search_state, "EstadoRepository", return_value=repo):
            result = await search_state.limpiar_estado_busqueda(
                _ctx(),
                search_state.LimpiarEstadoBusquedaInput(),
            )

        repo.delete.assert_awaited_once_with("5491112345678")
        self.assertEqual(result, {"limpiado": True})