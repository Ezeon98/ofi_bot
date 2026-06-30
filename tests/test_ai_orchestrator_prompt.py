"""Focused tests for AIOrchestrator prompt construction."""

from datetime import UTC, datetime
import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.models.response import (
    Intent,
    Message,
    MessageAction,
    ReplyButton,
)
from src.agents.prompts.router import ROUTER_SYSTEM_PROMPT
from src.memory.schemas import MemoryRead


def _load_provider_search_service_class():
    """Import the provider search service with a minimal pydantic_ai stub."""
    fake_pydantic_ai = ModuleType("pydantic_ai")
    fake_pydantic_ai.RunContext = object

    with patch.dict(sys.modules, {"pydantic_ai": fake_pydantic_ai}):
        module = import_module("src.application.services.provider_search_service")
    return module.ProviderSearchService


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
ProviderSearchService = _load_provider_search_service_class()


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


def test_resolve_requested_mode_prefers_explicit_metadata_mode() -> None:
    """Requested mode from UI metadata should win over text heuristics."""
    orchestrator = ai_orchestrator.AIOrchestrator.__new__(
        ai_orchestrator.AIOrchestrator
    )

    result = orchestrator._resolve_requested_mode(
        message="Buscar servicios",
        metadata={
            "button_id": "post_terms_offer_services",
            "requested_mode": ai_orchestrator.MODE_PROVIDER_PROFILE,
        },
        active_mode=None,
    )

    assert result["requested_mode"] == ai_orchestrator.MODE_PROVIDER_PROFILE
    assert result["effective_mode"] == ai_orchestrator.MODE_PROVIDER_PROFILE


def test_resolve_requested_mode_still_infers_from_free_text() -> None:
    """Explicit user text should still activate search mode without metadata."""
    orchestrator = ai_orchestrator.AIOrchestrator.__new__(
        ai_orchestrator.AIOrchestrator
    )

    result = orchestrator._resolve_requested_mode(
        message="Quiero buscar un servicio",
        metadata={"message_type": "text"},
        active_mode=None,
    )

    assert result["requested_mode"] == ai_orchestrator.MODE_PROVIDER_SEARCH
    assert result["effective_mode"] == ai_orchestrator.MODE_PROVIDER_SEARCH


def test_resolve_requested_mode_accepts_quiero_buscar_concrete_trade() -> None:
    """Concrete search phrasing should still be recognized before the LLM."""
    orchestrator = ai_orchestrator.AIOrchestrator.__new__(
        ai_orchestrator.AIOrchestrator
    )

    result = orchestrator._resolve_requested_mode(
        message="Quiero buscar un electricista",
        metadata={"message_type": "text"},
        active_mode=ai_orchestrator.MODE_PROVIDER_PROFILE,
    )

    assert result["requested_mode"] == ai_orchestrator.MODE_PROVIDER_SEARCH
    assert result["effective_mode"] == ai_orchestrator.MODE_PROVIDER_PROFILE


def test_router_prompt_requires_inference_from_problem_descriptions() -> None:
    """The agent prompt should tell the model to infer the trade from the issue."""
    assert "inferí el rubro más probable" in ROUTER_SYSTEM_PROMPT
    assert '"en mi casa"' in ROUTER_SYSTEM_PROMPT


def test_router_prompt_mentions_related_rubros_and_location_resolution_tools() -> None:
    """The agent prompt should expose the AI-owned search broadening workflow."""
    assert "tool_rubros_relacionados" in ROUTER_SYSTEM_PROMPT
    assert "tool_resolver_ubicacion" in ROUTER_SYSTEM_PROMPT
    assert "menos de 3 resultados" in ROUTER_SYSTEM_PROMPT


def test_router_prompt_mentions_system_question_intent() -> None:
    """System/product questions should map to a dedicated intent."""
    assert "**consultar_sistema**" in ROUTER_SYSTEM_PROMPT
    assert "cómo funciona esto" in ROUTER_SYSTEM_PROMPT


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
            agent_logging_enabled=False,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: ""),
            openai_api_key_secondary=SimpleNamespace(get_secret_value=lambda: ""),
            openai_model="gpt-4o-mini",
            openai_api_keys=lambda: tuple(),
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
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=AsyncMock(return_value=None),
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


class AIOrchestratorAgentSearchTests(IsolatedAsyncioTestCase):
    """Validate that service searches are now owned by the router agent."""

    def _settings(self) -> SimpleNamespace:
        """Build test settings with AI memory enabled."""
        return SimpleNamespace(
            memory_enabled=True,
            memory_max_memories=20,
            memory_max_tokens=2000,
            memory_summarize_after=50,
            memory_importance_threshold=0.7,
            agent_logging_enabled=False,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: ""),
            openai_api_key_secondary=SimpleNamespace(get_secret_value=lambda: ""),
            openai_model="gpt-4o-mini",
            openai_api_keys=lambda: tuple(),
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

    async def test_process_requests_location_via_agent(self) -> None:
        """A direct service request without a saved location should be answered by the agent."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        guided_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message=(
                "Para buscar plomero cerca tuyo, compartime tu ubicación de WhatsApp "
                "o escribime tu barrio o localidad."
            ),
            confidence=1.0,
            entities={"rubro": "plomero"},
            requires_action=False,
        )
        router_run = AsyncMock(
            return_value=SimpleNamespace(
                output=guided_response,
                new_messages=lambda: [],
            )
        )
        guided_search_mock = AsyncMock(return_value=None)
        location_update_mock = AsyncMock(return_value=None)

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=router_run),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=guided_search_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_location_update",
                new=location_update_mock,
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
        self.assertEqual(response.source, "llm")
        self.assertIn("compartime tu ubicación", response.message)
        router_run.assert_awaited_once()
        guided_search_mock.assert_not_awaited()
        location_update_mock.assert_not_awaited()

    async def test_process_uses_agent_results_when_location_exists(self) -> None:
        """Saved location still feeds context, but the agent owns the response."""
        now = datetime.now(UTC)
        memories = [
            MemoryRead(
                id=1,
                user_id=71,
                key="latitude",
                value="-34.6037",
                importance=0.95,
                created_at=now,
                updated_at=now,
            ),
            MemoryRead(
                id=2,
                user_id=71,
                key="longitude",
                value="-58.3816",
                importance=0.95,
                created_at=now,
                updated_at=now,
            ),
        ]
        memory_service = self._memory_service(memories)
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        provider_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="Encontré 1 plomero cerca de Caballito, CABA:",
            messages=[
                Message(
                    text="👤 Plomero Centro\n🔧 Plomería\n✅ Verificado\n📍 Caballito, CABA\n📏 2.1 km",
                    action=MessageAction(
                        type="cta_url",
                        label="Contactar",
                        url="https://api.whatsapp.com/send?phone=5491112345678&text=Hola%2C%20te%20contacto%20por%20MiOficio%20para%20consultar%20sobre%20tus%20servicios.&type=phone_number&app_absent=0",
                    ),
                )
            ],
            confidence=1.0,
            entities={"rubro": "plomero", "barrio": "Caballito", "ciudad": "CABA"},
            metadata={"providers": [
                {
                    "nombre": "Plomero Centro",
                    "rubros": ["Plomería"],
                    "barrio": "Caballito",
                    "ciudad": "CABA",
                    "telefono": "5491112345678",
                }
            ]},
        )
        router_run = AsyncMock(
            return_value=SimpleNamespace(
                output=provider_response,
                new_messages=lambda: [],
            )
        )
        guided_search_mock = AsyncMock(return_value=None)

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=router_run),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=guided_search_mock,
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
        self.assertEqual(response.source, "llm")
        self.assertEqual(
            response.message,
            "Encontramos 1 plomero que podrían ayudarte en Caballito:",
        )
        self.assertEqual(len(response.messages), 1)
        self.assertIn("Plomero Centro", response.messages[0]["text"])
        self.assertEqual(response.messages[0]["action"]["label"], "Contactar")
        router_run.assert_awaited_once()
        guided_search_mock.assert_not_awaited()


class AIOrchestratorModeRoutingTests(IsolatedAsyncioTestCase):
    """Validate top-level mode coordination before the mixed router agent."""

    def _settings(self) -> SimpleNamespace:
        """Build test settings with the same defaults as the other suites."""
        return SimpleNamespace(
            memory_enabled=True,
            memory_max_memories=20,
            memory_max_tokens=2000,
            memory_summarize_after=50,
            memory_importance_threshold=0.7,
            agent_logging_enabled=False,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: ""),
            openai_api_key_secondary=SimpleNamespace(get_secret_value=lambda: ""),
            openai_model="gpt-4o-mini",
            openai_api_keys=lambda: tuple(),
        )

    def _memory_service(self) -> SimpleNamespace:
        """Build the minimal memory service used by orchestrator tests."""
        return SimpleNamespace(
            get_memories=AsyncMock(return_value=[]),
            get_or_create_conversation=AsyncMock(
                return_value=SimpleNamespace(id=1, summary=None)
            ),
            get_recent_turns=AsyncMock(return_value=[]),
            process_interaction=AsyncMock(),
            upsert_memory=AsyncMock(),
        )

    async def test_process_confirms_switch_before_leaving_profile_mode(self) -> None:
        """An explicit search request should not immediately leave profile mode."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                return_value={
                    "active_mode": ai_orchestrator.MODE_PROVIDER_PROFILE,
                    "pending_mode": None,
                    "pending_confirmation": False,
                    "flows": {},
                }
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        router_run = AsyncMock()
        guided_search_mock = AsyncMock(return_value=None)
        registration_mock = AsyncMock(return_value=None)

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=registration_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=guided_search_mock,
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

        self.assertEqual(response.source, "mode_switch")
        self.assertIn("¿Querés cambiar a modo búsqueda de servicios?", response.message)
        state_repo.request_mode_switch.assert_awaited_once_with(
            "5491112345678",
            active_mode=ai_orchestrator.MODE_PROVIDER_PROFILE,
            pending_mode=ai_orchestrator.MODE_PROVIDER_SEARCH,
            pending_request={
                "message": "Necesito un plomero",
                "metadata": {"message_type": "text"},
            },
        )
        registration_mock.assert_not_awaited()
        guided_search_mock.assert_not_awaited()
        router_run.assert_not_awaited()

    async def test_process_replays_original_request_after_mode_confirmation(self) -> None:
        """After the user confirms, the stored request should run automatically."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                side_effect=[
                    {
                        "active_mode": ai_orchestrator.MODE_PROVIDER_PROFILE,
                        "pending_mode": ai_orchestrator.MODE_PROVIDER_SEARCH,
                        "pending_confirmation": True,
                        "pending_request": {
                            "message": "Quiero buscar un electricista",
                            "metadata": {"message_type": "text"},
                        },
                        "flows": {},
                    },
                    {
                        "active_mode": ai_orchestrator.MODE_PROVIDER_SEARCH,
                        "pending_mode": None,
                        "pending_confirmation": False,
                        "pending_request": None,
                        "flows": {},
                    },
                ]
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        guided_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="Encontré un electricista en Palermo.",
            confidence=1.0,
            entities={"rubro": "electricista", "barrio": "Palermo"},
        )
        location_update_mock = AsyncMock(return_value=None)
        guided_search_mock = AsyncMock(return_value=guided_response)
        router_run = AsyncMock()

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_location_update",
                new=location_update_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=guided_search_mock,
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
                message="SI",
                db=db,
                metadata={
                    "message_type": "interactive",
                    "interactive_type": "button_reply",
                    "selected_id": "mode_switch_yes",
                    "button_id": "mode_switch_yes",
                },
            )

        self.assertEqual(response.source, "shortcut")
        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO.value)
        self.assertEqual(response.message, "Encontré un electricista en Palermo.")
        state_repo.save_mode.assert_any_await(
            "5491112345678",
            active_mode=ai_orchestrator.MODE_PROVIDER_SEARCH,
            pending_mode=None,
            pending_confirmation=False,
            pending_request=None,
        )
        guided_search_mock.assert_awaited_once()
        router_run.assert_not_awaited()

    async def test_process_routes_to_guided_search_when_search_mode_is_active(self) -> None:
        """Search mode should prefer guided-search shortcuts before the router agent."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                return_value={
                    "active_mode": ai_orchestrator.MODE_PROVIDER_SEARCH,
                    "pending_mode": None,
                    "pending_confirmation": False,
                    "flows": {},
                }
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        guided_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="Encontré un plomero en Caballito.",
            confidence=1.0,
            entities={"rubro": "plomero", "barrio": "Caballito"},
        )
        location_update_mock = AsyncMock(return_value=None)
        guided_search_mock = AsyncMock(return_value=guided_response)
        registration_mock = AsyncMock(return_value=None)
        router_run = AsyncMock()

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=registration_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_location_update",
                new=location_update_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=guided_search_mock,
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

        self.assertEqual(response.source, "shortcut")
        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO.value)
        guided_search_mock.assert_awaited_once()
        location_update_mock.assert_awaited_once()
        registration_mock.assert_not_awaited()
        router_run.assert_not_awaited()

    async def test_process_routes_to_registration_when_profile_mode_is_active(self) -> None:
        """Profile mode should prefer the provider onboarding shortcut."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                return_value={
                    "active_mode": ai_orchestrator.MODE_PROVIDER_PROFILE,
                    "pending_mode": None,
                    "pending_confirmation": False,
                    "flows": {},
                }
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        registration_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.REGISTRAR_PRESTADOR,
            message="¿Cómo te llamás?",
            confidence=1.0,
        )
        registration_mock = AsyncMock(return_value=registration_response)
        guided_search_mock = AsyncMock(return_value=None)
        router_run = AsyncMock()

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=registration_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=guided_search_mock,
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
                message="Quiero ofrecer mis servicios",
                db=db,
                metadata={"button_id": ai_orchestrator.OFFER_SERVICES_BUTTON_ID},
            )

        self.assertEqual(response.source, "shortcut")
        self.assertEqual(response.intent, Intent.REGISTRAR_PRESTADOR.value)
        registration_mock.assert_awaited_once()
        guided_search_mock.assert_not_awaited()
        router_run.assert_not_awaited()

    async def test_process_routes_to_profile_update_before_agent(self) -> None:
        """Profile mode should run explicit profile update shortcuts before the agent."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                return_value={
                    "active_mode": ai_orchestrator.MODE_PROVIDER_PROFILE,
                    "pending_mode": None,
                    "pending_confirmation": False,
                    "flows": {},
                }
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        profile_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.ACTUALIZAR_PERFIL,
            message="Listo, sumé plomería a tu perfil de prestador.",
            confidence=1.0,
            entities={"rubros_agregados": ["Plomería"]},
        )
        registration_mock = AsyncMock(return_value=None)
        profile_update_mock = AsyncMock(return_value=profile_response)
        guided_search_mock = AsyncMock(return_value=None)
        router_run = AsyncMock()

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=registration_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderProfileService,
                "maybe_handle_profile_update",
                new=profile_update_mock,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=guided_search_mock,
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
                message="Tambien hago plomeria",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.source, "shortcut")
        self.assertEqual(response.intent, Intent.ACTUALIZAR_PERFIL.value)
        registration_mock.assert_awaited_once()
        profile_update_mock.assert_awaited_once()
        guided_search_mock.assert_not_awaited()
        router_run.assert_not_awaited()

    async def test_process_selects_profile_agent_for_llm_fallback(self) -> None:
        """Profile mode should use the provider-profile LLM agent when no shortcut applies."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                return_value={
                    "active_mode": ai_orchestrator.MODE_PROVIDER_PROFILE,
                    "pending_mode": None,
                    "pending_confirmation": False,
                    "flows": {},
                }
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        llm_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.CONSULTAR_ESTADO,
            message="Tu perfil sigue pendiente de revisión.",
            confidence=1.0,
        )
        profile_agent_run = AsyncMock(
            return_value=SimpleNamespace(output=llm_response, new_messages=lambda: [])
        )
        search_agent_run = AsyncMock()

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.ProviderProfileService,
                "maybe_handle_profile_update",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.router_agents,
                "provider_profile_agent",
                new=SimpleNamespace(run=profile_agent_run),
                create=True,
            ),
            patch.object(
                ai_orchestrator.router_agents,
                "provider_search_agent",
                new=SimpleNamespace(run=search_agent_run),
                create=True,
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Quiero saber el estado de mi perfil",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.source, "llm")
        self.assertEqual(response.message, "Tu perfil sigue pendiente de revisión.")
        profile_agent_run.assert_awaited_once()
        search_agent_run.assert_not_awaited()

    async def test_process_selects_search_agent_for_llm_fallback(self) -> None:
        """Search mode should use the search LLM agent when no shortcut applies."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                return_value={
                    "active_mode": ai_orchestrator.MODE_PROVIDER_SEARCH,
                    "pending_mode": None,
                    "pending_confirmation": False,
                    "flows": {},
                }
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        llm_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.CONSULTAR_SISTEMA,
            message="MiOficio conecta clientes con prestadores verificados.",
            confidence=1.0,
        )
        search_agent_run = AsyncMock(
            return_value=SimpleNamespace(output=llm_response, new_messages=lambda: [])
        )
        profile_agent_run = AsyncMock()

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_location_update",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.router_agents,
                "provider_profile_agent",
                new=SimpleNamespace(run=profile_agent_run),
                create=True,
            ),
            patch.object(
                ai_orchestrator.router_agents,
                "provider_search_agent",
                new=SimpleNamespace(run=search_agent_run),
                create=True,
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Cómo funciona MiOficio",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.source, "llm")
        search_agent_run.assert_awaited_once()
        profile_agent_run.assert_not_awaited()

    async def test_process_replays_message_after_agent_mode_handoff(self) -> None:
        """A tool-triggered mode switch should rerun the same turn once."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        state_repo = SimpleNamespace(
            get_mode=AsyncMock(
                return_value={
                    "active_mode": ai_orchestrator.MODE_PROVIDER_PROFILE,
                    "pending_mode": None,
                    "pending_confirmation": False,
                    "flows": {},
                }
            ),
            save_mode=AsyncMock(),
            request_mode_switch=AsyncMock(),
            clear_pending_mode=AsyncMock(),
        )
        first_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.AYUDA,
            message="Listo, cambié al modo búsqueda de servicios.",
            confidence=1.0,
        )
        second_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="Encontré electricistas cerca tuyo.",
            confidence=1.0,
            entities={"rubro": "electricista"},
        )

        async def profile_run(prompt: str, *, deps, model):
            deps.current_message_metadata = {
                "active_mode": ai_orchestrator.MODE_PROVIDER_SEARCH,
                "requested_mode_change": True,
                "agent_name": "provider_profile_agent",
            }
            return SimpleNamespace(output=first_response, new_messages=lambda: [])

        search_agent_run = AsyncMock(
            return_value=SimpleNamespace(output=second_response, new_messages=lambda: [])
        )

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_state_repo",
                return_value=state_repo,
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.ProviderProfileService,
                "maybe_handle_profile_update",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_reformat_provider_response",
                new=AsyncMock(side_effect=lambda response, **_: response),
            ),
            patch.object(
                ai_orchestrator.router_agents,
                "provider_profile_agent",
                new=SimpleNamespace(run=profile_run),
                create=True,
            ),
            patch.object(
                ai_orchestrator.router_agents,
                "provider_search_agent",
                new=SimpleNamespace(run=search_agent_run),
                create=True,
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Se me cortó la luz en casa",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.source, "llm")
        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO.value)
        self.assertEqual(response.message, "Encontré electricistas cerca tuyo.")
        self.assertEqual(search_agent_run.await_count, 1)
        state_repo.request_mode_switch.assert_not_awaited()


class AIOrchestratorSystemFallbackTests(IsolatedAsyncioTestCase):
    """Validate delegation of system questions to the documentation agent."""

    def _settings(self) -> SimpleNamespace:
        """Build test settings with AI memory enabled."""
        return SimpleNamespace(
            memory_enabled=True,
            memory_max_memories=20,
            memory_max_tokens=2000,
            memory_summarize_after=50,
            memory_importance_threshold=0.7,
            agent_logging_enabled=False,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: ""),
            openai_api_key_secondary=SimpleNamespace(get_secret_value=lambda: ""),
            openai_model="gpt-4o-mini",
            openai_api_keys=lambda: tuple(),
        )

    def _memory_service(self) -> SimpleNamespace:
        """Build a memory-service stub for orchestrator tests."""
        return SimpleNamespace(
            get_memories=AsyncMock(return_value=[]),
            get_or_create_conversation=AsyncMock(
                return_value=SimpleNamespace(id=1, summary=None)
            ),
            get_recent_turns=AsyncMock(return_value=[]),
            process_interaction=AsyncMock(),
            upsert_memory=AsyncMock(),
        )

    async def test_process_delegates_system_questions_to_documentation_agent(self) -> None:
        """A classified product question should be answered by the fallback agent."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        router_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.CONSULTAR_SISTEMA,
            message="Te respondo con la info del sistema.",
            confidence=0.92,
        )
        router_run = AsyncMock(
            return_value=SimpleNamespace(
                output=router_response,
                new_messages=lambda: [],
            )
        )
        fallback_answer = AsyncMock(
            return_value=(
                "MiOficio conecta clientes con prestadores verificados por "
                "WhatsApp y ofrece un plan Gratis y otro Verificado para "
                "prestadores."
            )
        )

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=router_run),
            ),
            patch.object(
                ai_orchestrator.SystemFallbackService,
                "answer",
                new=fallback_answer,
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="¿Cómo funciona MiOficio y cuánto sale el plan?",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.intent, Intent.CONSULTAR_SISTEMA.value)
        self.assertEqual(response.source, "llm")
        self.assertIn("prestadores verificados", response.message)
        router_run.assert_awaited_once()
        fallback_answer.assert_awaited_once()
        db.commit.assert_awaited_once()
        memory_service.process_interaction.assert_awaited_once()


class AIOrchestratorProviderFormattingTests(IsolatedAsyncioTestCase):
    """Validate provider-card formatting for agent-driven searches."""

    def _settings(self) -> SimpleNamespace:
        """Build test settings with AI memory enabled."""
        return SimpleNamespace(
            memory_enabled=True,
            memory_max_memories=20,
            memory_max_tokens=2000,
            memory_summarize_after=50,
            memory_importance_threshold=0.7,
            agent_logging_enabled=False,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: ""),
            openai_model="gpt-4o-mini",
        )

    def _memory_service(self) -> SimpleNamespace:
        """Build a memory-service stub for orchestrator tests."""
        return SimpleNamespace(
            get_memories=AsyncMock(return_value=[]),
            get_or_create_conversation=AsyncMock(
                return_value=SimpleNamespace(id=1, summary=None)
            ),
            get_recent_turns=AsyncMock(return_value=[]),
            process_interaction=AsyncMock(),
            upsert_memory=AsyncMock(),
        )

    async def test_process_rebuilds_provider_cards_from_tool_results(self) -> None:
        """Agent searches should keep one CTA card per provider even if the model collapses the text."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        collapsed_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message=(
                "Aquí tienes algunos electricistas cerca de Caballito:\n\n"
                "1. *Maria Electricista*\n"
                "- Zona: Caballito, CABA\n"
                "2. *Sofia Tecnica*\n"
                "- Zona: Castelar, Morón"
            ),
            confidence=1.0,
            entities={"rubro": "electricista", "barrio": "Caballito"},
            requires_action=True,
        )
        tool_messages = [
            SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        part_kind="tool-return",
                        tool_name="tool_buscar_prestadores",
                        content=[
                            {
                                "nombre": "Maria Electricista",
                                "rubros": ["Electricista domiciliario", "Gasista"],
                                "badge_verificado": True,
                                "barrio": "Caballito",
                                "ciudad": "CABA",
                                "telefono": "5491131046599",
                                "distance_km": 0.5,
                            },
                            {
                                "nombre": "Sofia Tecnica",
                                "rubros": ["Electricidad", "Tecnico en aire acondicionado"],
                                "badge_verificado": True,
                                "barrio": "Castelar",
                                "ciudad": "Morón",
                                "telefono": "5491100001006",
                                "distance_km": 18.7,
                            },
                        ],
                    )
                ]
            )
        ]
        router_run = AsyncMock(
            return_value=SimpleNamespace(
                output=collapsed_response,
                new_messages=lambda: tool_messages,
            )
        )

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=router_run),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=AsyncMock(return_value=None),
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Necesito un electricista en caballito",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO.value)
        self.assertEqual(response.source, "llm")
        self.assertEqual(
            response.message,
            "Encontramos 2 electricista que podrían ayudarte en Caballito:",
        )
        self.assertEqual(len(response.messages), 2)
        self.assertIn("Maria Electricista", response.messages[0]["text"])
        self.assertIn("Electricista domiciliario, Gasista", response.messages[0]["text"])
        self.assertIn("Sofia Tecnica", response.messages[1]["text"])
        self.assertEqual(response.messages[0]["action"]["label"], "Contactar")
        self.assertEqual(response.metadata["providers"][0]["nombre"], "Maria Electricista")

    async def test_process_merges_multiple_search_reports_into_one_provider_list(self) -> None:
        """Multiple related-rubro searches should merge into one outbound card list."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        collapsed_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="Encontré opciones para electricista en Lanús.",
            confidence=1.0,
            entities={"rubro": "electricista", "ciudad": "Lanús"},
            requires_action=True,
        )
        tool_messages = [
            SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        part_kind="tool-return",
                        tool_name="tool_buscar_prestadores",
                        content={
                            "status": "no_results",
                            "providers": [],
                            "provider_count": 0,
                            "related_rubros": ["Electricista domiciliario"],
                        },
                    ),
                    SimpleNamespace(
                        part_kind="tool-return",
                        tool_name="tool_buscar_prestadores",
                        content={
                            "status": "ok",
                            "providers": [
                                {
                                    "nombre": "Maria Electricista",
                                    "rubros": ["Electricista domiciliario"],
                                    "badge_verificado": True,
                                    "barrio": "Lanús Oeste",
                                    "ciudad": "Lanús",
                                    "telefono": "5491131046599",
                                },
                                {
                                    "nombre": "Sofia Tecnica",
                                    "rubros": ["Instalación de luminarias"],
                                    "badge_verificado": False,
                                    "barrio": "Lanús Este",
                                    "ciudad": "Lanús",
                                    "telefono": "5491100001006",
                                },
                            ],
                            "provider_count": 2,
                            "related_rubros": [],
                        },
                    ),
                    SimpleNamespace(
                        part_kind="tool-return",
                        tool_name="tool_buscar_prestadores",
                        content={
                            "status": "ok",
                            "providers": [
                                {
                                    "nombre": "Maria Electricista",
                                    "rubros": ["Electricista domiciliario"],
                                    "badge_verificado": True,
                                    "barrio": "Lanús Oeste",
                                    "ciudad": "Lanús",
                                    "telefono": "5491131046599",
                                },
                                {
                                    "nombre": "Lucas Porteros",
                                    "rubros": ["Porteros eléctricos"],
                                    "badge_verificado": True,
                                    "barrio": "Remedios de Escalada",
                                    "ciudad": "Lanús",
                                    "telefono": "5491100001010",
                                },
                            ],
                            "provider_count": 2,
                            "related_rubros": [],
                        },
                    ),
                ]
            )
        ]
        router_run = AsyncMock(
            return_value=SimpleNamespace(
                output=collapsed_response,
                new_messages=lambda: tool_messages,
            )
        )

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
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
                message="Necesito un electricista en Lanús",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(response.intent, Intent.BUSCAR_SERVICIO.value)
        self.assertEqual(len(response.messages), 3)
        self.assertIn("Maria Electricista", response.messages[0]["text"])
        self.assertIn("Sofia Tecnica", response.messages[1]["text"])
        self.assertIn("Lucas Porteros", response.messages[2]["text"])
        self.assertEqual(len(response.metadata["providers"]), 3)

    async def test_to_orchestrator_response_keeps_reply_buttons(self) -> None:
        """Reply-button actions should survive serialization to the bot layer."""
        response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="Encontré opciones.",
            messages=[
                Message(
                    text="Queres que te busque mas?",
                    action=MessageAction(
                        type="reply_buttons",
                        buttons=[
                            ReplyButton(id="provider_search_more_yes", title="SI"),
                            ReplyButton(id="provider_search_more_no", title="NO"),
                        ],
                    ),
                )
            ],
            confidence=1.0,
        )

        serialized = ai_orchestrator.AIOrchestrator._to_orchestrator_response(
            response,
            source="shortcut",
        )

        self.assertEqual(serialized.messages[0]["action"]["type"], "reply_buttons")
        self.assertEqual(
            serialized.messages[0]["action"]["buttons"],
            [
                {"id": "provider_search_more_yes", "title": "SI"},
                {"id": "provider_search_more_no", "title": "NO"},
            ],
        )

    async def test_process_rebuilds_provider_cards_ignoring_duplicate_block_after_results(self) -> None:
        """A duplicate-blocked retry should not hide the previous valid provider list."""
        memory_service = self._memory_service()
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        collapsed_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="No encontré plomeros en Castelar.",
            confidence=1.0,
            entities={"rubro": "plomero", "barrio": "Castelar"},
            requires_action=True,
        )
        tool_messages = [
            SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        part_kind="tool-return",
                        tool_name="tool_buscar_prestadores",
                        content=[
                            {
                                "nombre": "Juan Plomero",
                                "rubros": ["Plomeria"],
                                "badge_verificado": True,
                                "barrio": "Castelar",
                                "ciudad": "Morón",
                                "telefono": "5491100001001",
                                "distance_km": 1.4,
                            }
                        ],
                    ),
                    SimpleNamespace(
                        part_kind="tool-return",
                        tool_name="tool_buscar_prestadores",
                        content={
                            "info": "duplicate_call_blocked",
                            "message": "Ya buscaste plomero en Castelar.",
                            "rubro": "plomero",
                        },
                    ),
                ]
            )
        ]
        router_run = AsyncMock(
            return_value=SimpleNamespace(
                output=collapsed_response,
                new_messages=lambda: tool_messages,
            )
        )

        with (
            patch.object(ai_orchestrator, "build_openai_client", return_value=object()),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_build_memory_service",
                return_value=memory_service,
            ),
            patch.object(
                ai_orchestrator.AIOrchestrator,
                "_resolve_usuario_id",
                new=AsyncMock(return_value=71),
            ),
            patch.object(
                ai_orchestrator.ProviderRegistrationService,
                "maybe_handle_registration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                ai_orchestrator,
                "router_agent",
                new=SimpleNamespace(run=router_run),
            ),
            patch.object(
                ai_orchestrator.ProviderSearchService,
                "maybe_handle_guided_search",
                new=AsyncMock(return_value=None),
            ),
        ):
            orchestrator = ai_orchestrator.AIOrchestrator(self._settings())
            response = await orchestrator.process(
                user_id="5491112345678",
                message="Se rompio un caño en castelar",
                db=db,
                metadata={"message_type": "text"},
            )

        self.assertEqual(
            response.message,
            "Encontramos 1 plomero que podrían ayudarte en Castelar:",
        )
        self.assertEqual(len(response.messages), 1)
        self.assertIn("Juan Plomero", response.messages[0]["text"])