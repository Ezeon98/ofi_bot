# ServiMatch — Project Overview for AI Agents

## Resumen ejecutivo

ServiMatch (también llamado "ofi_bot" o "LaburáYA" en flujos legales) es un bot de WhatsApp que conecta clientes con prestadores de servicios verificados en Argentina. Combina un pipeline de IA basado en OpenIA/PydanticAI para entender intenciones con un sistema de búsqueda guiada de prestadores, membresías pagas con MercadoPago, persistencia de memoria por usuario y scheduling de tareas.

---

## Stack tecnológico

- **Lenguaje**: Python 3.12
- **Web framework / ASGI**: FastAPI + Uvicorn
- **IA / Agentes**: pydantic-ai (wrapper de OpenIA)
- **ORM**: SQLAlchemy 2.x (asyncpg / PostgreSQL)
- **Migraciones**: Alembic
- **Payments**: MercadoPago SDK (preapprovals)
- **WhatsApp**: WhatsApp Business Cloud API (Meta Graph API)
- **Geocoding**: Nominatim (OSM)
- **Scheduling**: APScheduler
- **Frontend**: HTML + JS + CSS (servido como `/suscripcion`)
- **Containerización**: Docker Compose (app, postgres, pgadmin)

---

## Arquitectura en capas

```
src/
├── presentation/          ← Entrada: API webhooks + bot handlers
│   ├── api/               ← REST / Webhook (FastAPI)
│   └── bot/               ← Lógica específica de WhatsApp
├── orchestrator/          ← Coordinador único del pipeline de IA
├── agents/                ← Definición del agente PydanticAI + tools
├── application/services/  ← Casos de uso (búsqueda, pagos, suscripciones)
├── context/               ← Construcción del contexto para el LLM
├── memory/                ← Memoria persistente por usuario (extraer, guardar, resumir)
├── tools/                 ← Herramientas LlmTools que el agente puede llamar
│   ├── business/          ← Búsqueda de prestadores + estado de búsqueda
│   └── memory/            ← Guardar/buscar/actualizar memoria
├── infrastructure/        ← Detalles técnicos (DB, WhatsApp client, queue, config)
│   ├── database/
│   │   └── repositories/  ← Repos (usuario, estado, mp_subscriptions)
│   ├── external/          ← WhatsApp client
│   └── queue/             ← Procesador asíncrono de webhook
├── schedulers/            ← Tareas programadas (limpieza, ejemplos)
└── utils/                 ← Geocoding, rate limiting, timezone, logging
```

**Regla de dependencia**: `presentation` → `orchestrator` → `agents` + `application/services` → `tools` → `infrastructure (repos)` → `database`. Nada en `tools` debe importar `orchestrator`.

---

## Flujo principal de un mensaje de WhatsApp

1. Meta envía un `POST` a `/wp_webhook`.
2. `src/presentation/api/main.py` recibe el webhook y encola las entradas en `process_webhook_entries`.
3. El queue procesa cada entrada, determina el tipo (texto, ubicación, botón) y llama a:
   - `src/presentation/bot/router.py::procesar_texto`
   - `src/presentation/bot/handlers/location.py::procesar_ubicacion`
   - `src/presentation/bot/terms_gate.py` (gate de términos y condiciones)
4. `procesar_texto` delega al **AIOrchestrator** (`src/orchestrator/ai_orchestrator.py`).
5. El orchestrator:
   - Carga memorias persistentes (`MemoryService`)
   - Construye el contexto (`ContextBuilder`)
   - Intenta un **shortcut de búsqueda guiada** (`ProviderSearchService`) — si aplica, devuelve directo sin llamar al LLM.
   - Ejecuta el `router_agent` (PydanticAI), que puede invocar tools (`buscar_prestadores`, `guardar_memoria`, etc.).
   - Persiste el turno en `conversation_turns`, actualiza memorias, limpia/prunea/quizás resume.
   - Devuelve un `OrchestratorResponse` estandarizado.
6. El router envía la respuesta por WhatsApp (`whatsapp_client`). Si hay `messages` con actions, envía un mensaje por provider con botón "Contactar".

---

## Módulo por módulo

### `src/presentation/api/main.py`
- App FastAPI.
- Lifespan: crea `/tmp/wpbase`, levanta scheduler, lo apaga al cerrar.
- Endpoints:
  - `GET /wp_webhook` — verificación de webhook de Meta.
  - `POST /wp_webhook` — recibe eventos de WhatsApp y los encola en `process_webhook_entries`.
  - `GET /suscripcion` — sirve frontend de suscripción.
  - Rutas de suscripciones en `subscriptions_router` (webhook de MercadoPago).

### `src/presentation/api/subscriptions.py`
- Router FastAPI para flujo de suscripciones a planes Pro/Premium.
- Expone endpoints de callback/status de MercadoPago.

### `src/presentation/bot/router.py`
- Recibe texto, ubicación, botones.
- Manda "typing..." antes de procesar.
- Llama a `AIOrchestrator.process(...)`.
- Decide si responder con `response.message` o con `response.messages` (botones CTA por proveedor).

### `src/presentation/bot/terms_gate.py`
- Guate de onboarding: envía PDF de términos, muestra botones Aceptar/Rechazar.
- Sólo deja pasar al flujo normal cuando `accepted_terms_at` está seteado.
- Constantes de IDs de botón (`terms_accept`, `post_terms_seek_services`, etc.).

### `src/presentation/bot/handlers/menu.py`
- Envía un menú principal genérico (placeholder de secciones y filas).
- TODO: personalizar con opciones reales.

### `src/presentation/bot/handlers/location.py`
- Recibe lat/lon de un mensaje de ubicación, hace reverse geocoding y responde con barrio + ciudad.

### `src/orchestrator/ai_orchestrator.py`
- **Entrada pública única** del pipeline de IA: `process(user_id, message, db, metadata)`.
- Orquesta:
  1. Obtención de memorias e historial reciente.
  2. Construcción de contexto (system prompt + historial).
  3. Inyección de dependencias (`AgentDependencies`).
  4. Shortcut de búsqueda guiada vía `ProviderSearchService`.
  5. Ejecución del `router_agent` si el shortcut no aplica.
  6. Post-procesamiento (reformateo de resultados de proveedores en multi-message).
  7. Persistencia de turno y commit.
  8. Traducción a `OrchestratorResponse`.

### `src/agents/router_agent.py`
- Singleton PydanticAI `Agent[AgentDependencies, AgentResponse]`.
- Tools registradas directamente (no registry dinámico):
  - `tool_buscar_prestadores` (con anti-loop 30s)
  - `tool_crear_prestador`
  - `tool_actualizar_prestador`
  - `tool_consultar_prestador`
  - `tool_consultar_estado_busqueda`
  - `tool_guardar_estado_busqueda`
  - `tool_limpiar_estado_busqueda`
  - `tool_guardar_memoria`
  - `tool_buscar_memoria`
  - `tool_actualizar_memoria`

### `src/agents/dependencies.py`
- `AgentDependencies` (dataclass): `db`, `user_id`, `memory_service`, `memory_config`, `current_message_metadata`.

### `src/agents/models/response.py`
- `Intent` (StrEnum): intenciones posibles (BUSCAR_SERVICIO, REGISTRAR_PRESTADOR, ACTUALIZAR_PERFIL, etc.).
- `AgentResponse`: contrato de salida del agente (`intent`, `message`, `messages`, `confidence`, `entities`, `requires_action`, `metadata`).

### `src/agents/prompts/router.py`
- `ROUTER_SYSTEM_PROMPT`: define el comportamiento del bot (español rioplatense, reglas de búsqueda guiada, NEVER reintentar búsquedas).

### `src/application/services/provider_search_service.py`
- Servicio que encapsula todo el flujo de búsqueda guiada (antes estaba en orchestrator).
- Estados: `awaiting_need` → `awaiting_zone` → resultados.
- Extrae rubro/zona de texto, usa metadatos, memoria y geocoding.
- Formatea resultados en mensajes + botones CTA de WhatsApp.
- Persiste ubicación en memoria para futuras búsquedas.

### `src/application/services/mercadopago_service.py`
- Servicio de integración con MercadoPago (preapprovals, información de cuenta).
- Crea preferencias, maneja webhooks.

### `src/application/services/subscription_service.py`
- Orquesta planes Pro/Premium, precios y lógica de gracia/renovación.
- Orquesta `ProviderSearchService` para rankear por plan cuando corresponde.

### `src/tools/business/providers.py`
- Herramientas para CRUD de perfil de prestador.
- `buscar_prestadores`:
  - Join providers + usuarios.
  - Filtros por texto (barrio/ciudad) o coordenadas (ranking por distancia Haversine).
  - Filtro por rubro/trade (sinónimos expansibles).
  - Ordena por badge_activo y plan.
  - Devuelve lista enriquecida (rubros normalizados, distancia, teléfono, badge, facturación).

### `src/tools/business/search_state.py`
- Estado de búsqueda guiada por usuario (`guided_provider_search`).
- CRUD sobre `EstadoRepository` (consultar/guardar/limpiar).

### `src/tools/memory/memory_tools.py`
- Tools expuestas al LLM para guardar, buscar, actualizar memorias del usuario.

### `src/tools/registry/registry.py`
- `ToolRegistry` genérico para registrar/instalar tools en un agent. Usado como patrón base; actualmente no es el mecanismo activo de registro directo.

### `src/context/builder.py`
- Toma memorias + historial reciente y arma el system context que se envía al LLM (control de tokens).

### `src/memory/service.py`
- `MemoryService`: facade de memoria por request.
- `get_memories`, `get_or_create_conversation`, `get_recent_turns`, `process_interaction` (upsert memorias + turno + prune/resumen).

### `src/memory/extractor.py`
- `MemoryExtractor`: usa `gpt-4o-mini` para extraer facts clave de un mensaje de usuario.

### `src/memory/summarizer.py`
- `MemorySummarizer`: usa `gpt-4o-mini` para condensar turnos viejos cuando se supera `memory_summarize_after`.

### `src/memory/models.py`
- `MemoryConfig`: flags de la capa de memoria (habilitada, límites, umbral de importancia).

### `src/memory/repository.py`
- Repo de acceso a `user_memories` y `conversations`/`conversation_turns`.

### `src/memory/schemas.py`
- DTOs (`MemoryRead`, etc.) para transferir datos de memoria entre capas.

### `src/infrastructure/config.py`
- `Settings` (Pydantic Settings): configuración central.
  - WhatsApp, MercadoPago, OpenAI, DB, memoria, rate limiting, precios, planes.
  - Propiedades computadas: `meta_api_url`, `meta_media_url`, `meta_headers`.

### `src/infrastructure/container.py`
- `UnitOfWork`: agrega repos (`usuarios`, `estados`, `mp_subscriptions`) sobre una `AsyncSession`.

### `src/infrastructure/database/session.py`
- Motor async + `get_session` (dependency FastAPI) queYield una `AsyncSession`por request con `commit`/`rollback` automáticos.

### `src/infrastructure/database/models.py`
- ORM models:
  - `UsuarioModel`, `EstadoUsuarioModel`, `MessageCountModel`.
  - `MercadoPagoSubscriptionModel`.
  - `ProviderModel`, `TradeModel`, `ProviderTradeModel`, `ProviderRatingModel`.
  - `UserMemoryModel`, `ConversationModel`, `ConversationTurnModel`.

### `src/infrastructure/database/repositories/usuario.py`
- `UsuarioRepository`: CRUD y helpers para `usuarios` (telefono, tier, bsuid, accepted_terms_at, mp_*).

### `src/infrastructure/database/repositories/estado.py`
- `EstadoRepository`: CRUD genérico sobre columna `datos` JSONB-like en `usuario_estado`.

### `src/infrastructure/database/repositories/mp_subscriptions.py`
- Repo de suscripciones MP (find active, upsert, etc.).

### `src/infrastructure/external/whatsapp_client.py`
- Helpers HTTP contra la Graph API de Meta:
  - `enviar_mensaje`, `enviar_typing`, `enviar_boton_cta`, `enviar_botones_respuesta`, `enviar_documento`, `enviar_lista_interactiva`.
  - `build_whatsapp_contact_url`: arma `wa.me` con mensaje predefinido.

### `src/infrastructure/queue/processor.py`
- `process_webhook_entries`: descompone el body de Meta en entradas (texto, ubicación, botón) y las despacha a handlers.

### `src/audio/transcription.py`
- Convierte nota de voz a texto (Whisper u otro backend).

### `src/schedulers/scheduler.py`
- `iniciar_scheduler`: levanta APScheduler en background al iniciar la app.

### `src/schedulers/registry.py`
- Define las tareas registrables (limpieza de conversaciones, etc.).

### `src/schedulers/tasks/base.py`
- Clase base de tareas con helpers comunes (inyección de sesión).

### `src/schedulers/tasks/cleanup_old_conversations.py`
- Borra conversaciones antiguas y turnos asociados para no crecer infinitamente.

### `src/schedulers/tasks/example_task.py`
- Tarea ejemplo/baseline.

### `src/utils/geocoding.py`
- `geocode_text_location`: geocodifica texto ("Barrio X, Ciudad Y") vía Nominatim.
- `reverse_geocode_location`: lat/lon → datos de ciudad/barrio.

### `src/utils/rate_limiter.py`
- Rate limiting por usuario (anti-abuso en llamadas a la API).

### `src/utils/timezone.py`
- Helper para trabajar con zonas horarias de Argentina.

### `src/utils/agent_logger.py`
- `AgentLogger`: guarda evidencia estructurada de cada turno en `agent.log` (JSON lines).

### `tests/`
- Unit/integration tests de router, schemas, ranking, search state, terms gate, orchestrator prompts.
- Correr: `pytest`.

### `docker-compose.yml`
- Orquesta: app (8000), postgres, pgadmin.
- Volúmenes: código, datos postgres.

### `Dockerfile`
- Imagen Python + dependencias.
- Expone 8000, comando `uvicorn`.

### `alembic.ini` + `alembic/versions/`
- Migraciones: usuarios, ai_layer, terms, providers/trades/catalog, state/msg counts, cleanup, ratings.

### `frontend/`
- `index.html`, `app.js`, `styles.css`: formulario de suscripción (CardForm embebido de MercadoPago).

---

## Entidades y flujos clave

### Usuarios
- **Cliente**: solo busca servicios, no requiere pago (free).
- **Prestador**: registra perfil, rubros, zona. Plan free o Verificado (pago).
- **BSUID**: migración a Business-Scoped User ID de Meta.

### Estados / State Machine
- `user_estado.usuario_estado`: JSON genérico para states de búsqueda guiada (`guided_provider_search` pasos `awaiting_need`, `awaiting_zone`).
- Se limpia automáticamente luego de buscar o abandonar.

### Búsqueda guiada
1. Usuario pide "Busco plomero en".
2. Sistema detecta intención (shortcut o LLM).
3. Persiste estado `awaiting_need` o `awaiting_zone`.
4. Sigue turno a turno hasta tener rubro + zona.
5. Ejecuta `buscar_prestadores` (geocoding + filtros + ranking).
6. Devuelve mensaje + lista de Message con botones CTA `wa.me/...`.

### Prestadores y Trades
- `ProviderModel`: perfil con rubros (legacy JSON), plan, badge, activo, coordenadas, facturación.
- `TradeModel` + `ProviderTradeModel`: catálogo normalizado (trade).
- Sinónimos de rubros mapeados a stems para búsquedas tolerantes.

### Suscripciones
- Planes Pro/Mensual-Anual, Premium/Mensual-Anual.
- Precios hardcodeados en Settings (ARS) o configurables.
- MP Preapprovals en `mp_subscriptions`.
- Renovación, gracia, webhooks de MP servidos en FastAPI.

### Memoria
- `user_memories`: key-value con importance.
- `conversations` + `conversation_turns`: historial reciente.
- `MemoryExtractor` + `MemorySummarizer`: usa GPT para extraer/resumir.
- Límites: `memory_max_memories`, `memory_max_tokens`, `memory_summarize_after`.

### Anti-loop
- `router_agent` guarda última firma de tool call (30s).
- Bloquea la reintentada de `tool_buscar_prestadores` con mismos params si ya devolvió 0 resultados.

---

## Variables de entorno relevantes (`.env`)

```env
# WhatsApp
WHATSAPP_TOKEN=<token de Meta>
PHONE_NUMBER_ID=<id del número>
WABA_ID=<opcional>
VERIFY_TOKEN=<token para verificar webhook>
BOT_PHONE_NUMBER=<número visible, ej +549...

# MercadoPago
MP_ACCESS_TOKEN=<access token>
MP_PUBLIC_KEY=<public key>
MP_WEBHOOK_SECRET=<firma de webhook>

# DB
DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/whatsapp_bot

# AI
OPENAI_API_KEY=<clave OpenAI>
OPENAI_MODEL=gpt-4o-mini
AI_ENABLED=true
AGENT_LOGGING_ENABLED=true

# Memoria
MEMORY_ENABLED=true
MEMORY_MAX_MEMORIES=20
MEMORY_MAX_TOKENS=2000
MEMORY_SUMMARIZE_AFTER=50
MEMORY_IMPORTANCE_THRESHOLD=0.7

# App
DEBUG=false
PORT=8000
TMP_DIR=/tmp/wpbase
BASE_URL=https://tu-dominio.com

# Rate limiting
RATE_LIMIT=20
RATE_WINDOW=60

# Precios / Planes (opcional si no usás defaults)
PRO_MONTHLY_PRICE=3999
PRO_ANNUAL_PRICE=39990
PREMIUM_MONTHLY_PRICE=6999
PREMIUM_ANNUAL_PRICE=69990

# Plan IDs de MercadoPago (opcional)
MP_PLAN_MONTHLY_TRIAL=<id>
MP_PLAN_MONTHLY_NO_TRIAL=<id>
MP_PLAN_ANNUAL_TRIAL=<id>
MP_PLAN_ANNUAL_NO_TRIAL=<id>
MP_PLAN_ID=<id legacy>
MP_PLAN_ID_NO_TRIAL=<id legacy>

# Planes Premium (opcional)
MP_PREMIUM_MONTHLY_TRIAL=<id>
...
```

---

## Puntos de entrada para modificar cada módulo

- **Cambiar comportamiento del bot**: editar `src/agents/prompts/router.py`.
- **Agregar una tool para el LLM**: definir schema Pydantic + async fn, decorar `@router_agent.tool` en `src/agents/router_agent.py`.
- **Cambiar lógica de búsqueda**: `src/tools/business/providers.py` y `src/application/services/provider_search_service.py`.
- **Cambiar precios/planes**: `src/infrastructure/config.py` (valores por defecto) y `src/application/services/subscription_service.py`.
- **Modificar frontend de suscripción**: `frontend/` (HTML/JS/CSS).
- **Tareas programadas**: `src/schedulers/tasks/`.
- **Nuevos endpoints**: `src/presentation/api/`.

---

## Flujo práctico: búsqueda guiada paso a paso

| Paso | Usuario | Sistema |
|------|---------|---------|
| 1 | "Busco plomero en Villa Urquiza" | `ProviderSearchService` detecta rubro+zona, ejecuta `buscar_prestadores`, devuelve `AgentResponse` con `messages` (un `Message` por proveedor + botón "Contactar"). |
| 2 | (sin intervención, el router envía cada mensaje individualmente) | `router.py` recorre `response.messages`, envía un WhatsApp por proveedor via `enviar_boton_cta`. Limpia el estado de búsqueda. |
| 3 | "Necesito gasista" (después de ver resultados) | Nuevo turno; si no hay zona, guarda estado `awaiting_zone` y pide ubicación. |
| 4 | "Ensayos" (zona) | Recupera estado, completa zona, ejecuta búsqueda, devuelve resultados. |

---

## Notas para IA/Codebase Agents

1. El **único punto de entrada** del pipeline de IA es `AIOrchestrator.process(...)`. Todo lo demás son detalles internos.
2. Los nombres de tools y parámetros son **contratos con el LLM**; cambiararlos exige actualizar el system prompt correspondiente.
3. `turn_id` es un identificador correlativo por request, usado solo para logging estructurado.
4. Los IDs de botón de WhatsApp (`terms_accept`, `post_terms_seek_services`, etc.) se usan en el switch de `terms_gate` y en `provider_search_service` (`SEARCH_BUTTON_ID`); hay que mantenerlos sincronizados.
5. Geocoding usa Nominatim público: respetar rate limits, cachear cuando sea posible.
6. La base de datos persiste:
   - usuarios, perfiles de prestador, trades.
   - suscripciones de MercadoPago.
   - estados de búsqueda guiada.
   - memorias y conversaciones.
7. Las migraciones Alembic están en `alembic/versions/`.
8. Para ejecutar localmente (modo hot reload): `python main.py` (expone 8000).