"""Focused tests for AIOrchestrator prompt construction."""

from datetime import UTC, datetime
import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.agents.models.response import Intent


def _load_orchestrator_module():
    fake_router_module = ModuleType("src.agents.router_agent")
    fake_router_module.router_agent = object()
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object

    with patch.dict(
        sys.modules,
        {
            "src.agents.router_agent": fake_router_module,
            "pydantic_ai": fake_pydantic_ai,
        },
    ):
        return import_module("src.orchestrator.ai_orchestrator")


ai_orchestrator = _load_orchestrator_module()


def test_build_user_prompt_includes_metadata_block() -> None:
    """Structured metadata should be injected into the agent prompt."""
    prompt = ai_orchestrator.AIOrchestrator._build_user_prompt(
        "Quiero un plomero",
        "## Historial reciente\nUSER: hola",
        {"message_type": "button_reply", "button_id": "post_terms_seek_services"},
    )

    assert "## Historial reciente" in prompt
    assert "## Metadata del mensaje" in prompt
    assert "post_terms_seek_services" in prompt
    assert "Mensaje actual del usuario: Quiero un plomero" in prompt


class AIOrchestratorFailureTests(IsolatedAsyncioTestCase):
    """Validate error handling around agent execution and persistence."""

    async def test_process_rolls_back_and_returns_fallback_when_agent_fails(self) -> None:
        """A tool or SQL failure during agent execution should not poison the whole request."""
        settings = SimpleNamespace(
            memory_enabled=True,
            memory_max_memories=20,
            memory_max_tokens=2000,
            memory_summarize_after=50,
            memory_importance_threshold=0.7,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: ""),
            openai_model="gpt-4o-mini",
        )
        memory_service = SimpleNamespace(
            get_memories=AsyncMock(return_value=[]),
            get_or_create_conversation=AsyncMock(return_value=SimpleNamespace(id=1, summary=None)),
            get_recent_turns=AsyncMock(return_value=[]),
            process_interaction=AsyncMock(),
        )
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        fake_result = RuntimeError("db failure")

        with (
            patch.object(ai_orchestrator, "AsyncOpenAI", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator,
                "EstadoRepository",
                return_value=SimpleNamespace(
                    get=AsyncMock(return_value={}),
                    save=AsyncMock(),
                    delete=AsyncMock(),
                ),
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=AsyncMock(side_effect=fake_result)),
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(settings)
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Hola",
                db=db,
            )

        self.assertEqual(response.intent, Intent.CONVERSACION_GENERAL.value)
        self.assertEqual(response.confidence, 0.0)
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
        memory_service.process_interaction.assert_not_awaited()


class AIOrchestratorSearchShortcutTests(IsolatedAsyncioTestCase):
    """Validate the deterministic guided search path for service requests."""

    def _settings(self) -> SimpleNamespace:
        """Build test settings with AI memory enabled."""
        return SimpleNamespace(
            memory_enabled=True,
            memory_max_memories=20,
            memory_max_tokens=2000,
            memory_summarize_after=50,
            memory_importance_threshold=0.7,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: ""),
            openai_model="gpt-4o-mini",
        )

    def _memory_service(self, memories: list | None = None) -> SimpleNamespace:
        """Build a memory-service stub for orchestrator tests."""
        return SimpleNamespace(
            get_memories=AsyncMock(return_value=memories or []),
            get_or_create_conversation=AsyncMock(
                return_value=SimpleNamespace(id=1, summary=None)
            ),
            get_recent_turns=AsyncMock(return_value=[]),
            process_interaction=AsyncMock(),
            upsert_memory=AsyncMock(),
        )

    async def test_process_requests_location_before_running_agent(self) -> None:
        """A direct service request without a saved location should ask for one."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get=AsyncMock(return_value={}),
            save=AsyncMock(),
            delete=AsyncMock(),
        )
        router_run = AsyncMock()

        with (
            patch.object(ai_orchestrator, "AsyncOpenAI", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator,
                "EstadoRepository",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=router_run),
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Necesito un plomero",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO.value)
        self.assertIn("compartime tu ubicación", response.message)
        state_repo.save.assert_awaited_once_with(
            "5491112345678",
            {
                "estado": ai_orchestrator.SEARCH_STATE_NAME,
                "paso": "awaiting_zone",
                "rubro": "plomero",
                "zona": None,
                "detalle": None,
            },
        )
        router_run.assert_not_awaited()
        db.commit.assert_awaited_once()

    async def test_process_uses_saved_location_and_returns_results(self) -> None:
        """A saved location should be enough to search providers without the agent."""
        now = datetime.now(UTC)
        memories = [
            ai_orchestrator.MemoryRead(
                id=1,
                user_id="5491112345678",
                key="search_latitude",
                value="-34.6037",
                importance=0.95,
                created_at=now,
                updated_at=now,
            ),
            ai_orchestrator.MemoryRead(
                id=2,
                user_id="5491112345678",
                key="search_longitude",
                value="-58.3816",
                importance=0.95,
                created_at=now,
                updated_at=now,
            ),
            ai_orchestrator.MemoryRead(
                id=3,
                user_id="5491112345678",
                key="search_zona",
                value="Caballito, CABA",
                importance=0.85,
                created_at=now,
                updated_at=now,
            ),
        ]
        memory_service = self._memory_service(memories)
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get=AsyncMock(return_value={}),
            save=AsyncMock(),
            delete=AsyncMock(),
        )
        router_run = AsyncMock()
        buscar_prestadores = AsyncMock(
            return_value=[
                {
                    "nombre": "Plomero Centro",
                    "rubros": ["Plomería"],
                    "zona": "Caballito, CABA",
                    "ciudad": "CABA",
                    "barrio": "Caballito",
                    "lat": -34.6183,
                    "lon": -58.4432,
                    "disponibilidad": "Guardias",
                    "badge_verificado": True,
                    "facturacion": "monotributo",
                    "distance_km": 2.1,
                }
            ]
        )

        with (
            patch.object(ai_orchestrator, "AsyncOpenAI", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator,
                "EstadoRepository",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator,
                "buscar_prestadores",
                new=buscar_prestadores,
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=router_run),
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Necesito un plomero",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO.value)
        self.assertIn("Plomero Centro", response.message)
        self.assertIn("2.1 km", response.message)
        self.assertEqual(buscar_prestadores.await_count, 1)
        params = buscar_prestadores.await_args.args[1]
        self.assertEqual(params.rubro, "plomero")
        self.assertEqual(params.zona, "Caballito, CABA")
        self.assertEqual(params.lat, -34.6037)
        self.assertEqual(params.lon, -58.3816)
        state_repo.delete.assert_awaited_once_with("5491112345678")
        router_run.assert_not_awaited()
        db.commit.assert_awaited_once()