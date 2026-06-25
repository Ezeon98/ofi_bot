"""Focused tests for AIOrchestrator prompt construction."""

from datetime import UTC, datetime
import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.models.response import Intent, Message, MessageAction
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


def test_router_prompt_requires_inference_from_problem_descriptions() -> None:
    """The agent prompt should tell the model to infer the trade from the issue."""
    assert "inferí el rubro más probable" in ROUTER_SYSTEM_PROMPT
    assert '"en mi casa"' in ROUTER_SYSTEM_PROMPT


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
            agent_logging_enabled=False,
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
        router_run = AsyncMock()
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

        with (
            patch.object(ai_orchestrator, "AsyncOpenAI", return_value=object()),
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
                new=AsyncMock(return_value=guided_response),
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
        self.assertEqual(response.source, "shortcut")
        self.assertIn("compartime tu ubicación", response.message)
        router_run.assert_not_awaited()

    async def test_process_uses_saved_location_and_returns_results(self) -> None:
        """A saved location should be enough to search providers without the agent."""
        now = datetime.now(UTC)
        memories = [
            MemoryRead(
                id=1,
                user_id=71,
                key="search_latitude",
                value="-34.6037",
                importance=0.95,
                created_at=now,
                updated_at=now,
            ),
            MemoryRead(
                id=2,
                user_id=71,
                key="search_longitude",
                value="-58.3816",
                importance=0.95,
                created_at=now,
                updated_at=now,
            ),
        ]
        memory_service = self._memory_service(memories)
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        router_run = AsyncMock()
        provider_response = ai_orchestrator.AgentResponse(
            intent=ai_orchestrator.Intent.BUSCAR_SERVICIO,
            message="Encontré 1 plomero cerca de Caballito, CABA:",
            messages=[
                Message(
                    text="👤 Plomero Centro\n🔧 Plomería\n✅ Verificado\n📍 Caballito, CABA\n📏 2.1 km",
                    action=MessageAction(
                        type="cta_url",
                        label="Contactar",
                        url="https://api.whatsapp.com/send?phone=5491112345678&text=Hola%2C%20te%20contacto%20por%20ServiMatch%20para%20consultar%20sobre%20tus%20servicios.&type=phone_number&app_absent=0",
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

        with (
            patch.object(ai_orchestrator, "AsyncOpenAI", return_value=object()),
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
                new=AsyncMock(return_value=provider_response),
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
        self.assertEqual(response.source, "shortcut")
        self.assertEqual(response.message, "Encontré 1 plomero cerca de Caballito, CABA:")
        self.assertEqual(len(response.messages), 1)
        self.assertIn("Plomero Centro", response.messages[0]["text"])
        self.assertIn("2.1 km", response.messages[0]["text"])
        router_run.assert_not_awaited()


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
                                "rubros": ["Electricidad"],
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
            patch.object(ai_orchestrator, "AsyncOpenAI", return_value=object()),
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
        self.assertIn("Sofia Tecnica", response.messages[1]["text"])
        self.assertEqual(response.messages[0]["action"]["label"], "Contactar")
        self.assertEqual(response.metadata["providers"][0]["nombre"], "Maria Electricista")

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
            patch.object(ai_orchestrator, "AsyncOpenAI", return_value=object()),
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