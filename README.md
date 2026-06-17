# WhatsApp Bot Base — Clean Boilerplate

A minimal, production-ready WhatsApp Business bot with MercadoPago subscription
payments. Built with **FastAPI**, **SQLAlchemy async**, **PostgreSQL**, and
**Docker**.

## Architecture

```
wp_base/
├── src/
│   ├── application/services/       # Business logic (subscriptions)
│   ├── infrastructure/
│   │   ├── config.py               # Pydantic settings from .env
│   │   ├── container.py            # Unit of Work (dependency injection)
│   │   ├── database/               # Models, session, repositories
│   │   └── external/               # WhatsApp Cloud API client
│   ├── presentation/
│   │   ├── api/main.py             # FastAPI app + webhook
│   │   ├── api/subscriptions.py    # MercadoPago endpoints
│   │   └── bot/                    # Router + handlers (add your logic here)
│   └── utils/                      # Timezone, rate limiter
├── frontend/                       # MercadoPago payment page
├── alembic/                        # DB migrations
├── Dockerfile
├── docker-compose.yml
└── bootstrap.sh
```

## Quick Start

1. Copy `.env.example` to `.env` and fill in your keys
2. `docker compose up --build`
3. Set your WhatsApp webhook URL to `https://your-domain/wp_webhook`

## Adding Bot Features

Edit `src/presentation/bot/router.py` → `procesar_texto()` and add handler
modules in `src/presentation/bot/handlers/`.

The state machine stores per-user state in `usuario_estado` table via
`uow.estados.save/get/delete`.

## Environment Variables

See `.env.example` for the full list.

## Commands

| Action | Command |
|--------|---------|
| Run locally | `uvicorn src.presentation.api.main:app --port 8000` |
| Docker | `docker compose up --build` |
| Create migration | `alembic revision --autogenerate -m "description"` |
| Apply migrations | `alembic upgrade head` |
