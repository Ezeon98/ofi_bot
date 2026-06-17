"""Business tools: provider operations.

Each function represents a BUSINESS ACTION, not a generic DB operation.
The LLM calls these by name; names must be self-documenting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from sqlalchemy import select, update

from src.agents.dependencies import AgentDependencies
from src.infrastructure.database.models import ProviderModel

logger = logging.getLogger(__name__)


# ── Input schemas ────────────────────────────────────────────────────────────


class BuscarPrestadoresInput(BaseModel):
    rubro: str = Field(description="Service category, e.g. 'plomero', 'electricista'")
    zona: str | None = Field(default=None, description="Neighborhood or city area")
    solo_verificados: bool = Field(default=False)
    limit: int = Field(default=5, ge=1, le=10)


class CrearPrestadorInput(BaseModel):
    nombre: str
    rubros: list[str] = Field(min_length=1, max_length=2)
    zona: str
    disponibilidad: str | None = None
    experiencia: str | None = Field(default=None, max_length=500)
    facturacion: str = Field(default="no_factura")


class ActualizarPrestadorInput(BaseModel):
    field: str = Field(description="Field to update: disponibilidad, experiencia, zona, rubros")
    value: str = Field(description="New value (rubros as JSON array string)")


class ConsultarPrestadorInput(BaseModel):
    pass  # uses user_id from deps


# ── Tool implementations ─────────────────────────────────────────────────────


async def buscar_prestadores(
    ctx: RunContext[AgentDependencies],
    params: BuscarPrestadoresInput,
) -> list[dict[str, Any]]:
    """Find active service providers matching a rubro and optional zone.

    Returns up to `limit` results, verified providers first.
    """
    stmt = select(ProviderModel).where(ProviderModel.activo == True)  # noqa: E712

    if params.solo_verificados:
        stmt = stmt.where(ProviderModel.badge_activo == True)  # noqa: E712

    if params.zona:
        stmt = stmt.where(ProviderModel.zona.ilike(f"%{params.zona}%"))

    stmt = (
        stmt.order_by(
            ProviderModel.badge_activo.desc(),
            ProviderModel.plan.desc(),
        )
        .limit(params.limit)
    )

    rows = list(await ctx.deps.db.scalars(stmt))
    results = []
    for r in rows:
        rubros = json.loads(r.rubros) if r.rubros else []
        if any(params.rubro.lower() in rub.lower() for rub in rubros) or not rubros:
            results.append(
                {
                    "nombre": r.nombre,
                    "rubros": rubros,
                    "zona": r.zona,
                    "disponibilidad": r.disponibilidad,
                    "badge_verificado": r.badge_activo,
                    "facturacion": r.facturacion,
                }
            )
    return results


async def crear_prestador(
    ctx: RunContext[AgentDependencies],
    params: CrearPrestadorInput,
) -> dict[str, Any]:
    """Register a new service provider profile for the current user.

    Returns the created provider summary.
    """
    from sqlalchemy import select as sa_select

    # Prevent duplicates
    existing = await ctx.deps.db.scalar(
        sa_select(ProviderModel).where(
            ProviderModel.usuario_id == int(ctx.deps.user_id)
            if ctx.deps.user_id.isdigit()
            else ProviderModel.usuario_id == 0  # fallback: telefono lookup needed
        )
    )
    if existing:
        return {"error": "Ya existe un perfil de prestador para este usuario.", "id": existing.id}

    provider = ProviderModel(
        usuario_id=int(ctx.deps.user_id) if ctx.deps.user_id.isdigit() else 0,
        nombre=params.nombre,
        rubros=json.dumps(params.rubros, ensure_ascii=False),
        zona=params.zona,
        disponibilidad=params.disponibilidad,
        experiencia=params.experiencia,
        facturacion=params.facturacion,
        plan="free",
        activo=False,  # needs manual review
    )
    ctx.deps.db.add(provider)
    await ctx.deps.db.flush()
    return {"id": provider.id, "nombre": provider.nombre, "estado": "pendiente_revision"}


async def actualizar_prestador(
    ctx: RunContext[AgentDependencies],
    params: ActualizarPrestadorInput,
) -> dict[str, Any]:
    """Update a single field of the current user's provider profile."""
    allowed_fields = {"disponibilidad", "experiencia", "zona", "rubros"}
    if params.field not in allowed_fields:
        return {"error": f"Campo no permitido: {params.field}"}

    value: Any = params.value
    if params.field == "rubros":
        try:
            value = json.dumps(json.loads(params.value), ensure_ascii=False)
        except json.JSONDecodeError:
            return {"error": "El campo rubros debe ser un JSON array de strings."}

    result = await ctx.deps.db.execute(
        update(ProviderModel)
        .where(
            ProviderModel.usuario_id == int(ctx.deps.user_id)
            if ctx.deps.user_id.isdigit()
            else ProviderModel.usuario_id == -1
        )
        .values(**{params.field: value})
        .returning(ProviderModel.id)
    )
    updated_id = result.scalar_one_or_none()
    if updated_id is None:
        return {"error": "No se encontró perfil de prestador para este usuario."}
    return {"updated": True, "field": params.field}


async def consultar_prestador(
    ctx: RunContext[AgentDependencies],
    _params: ConsultarPrestadorInput,
) -> dict[str, Any]:
    """Return the current user's provider profile, if any."""
    row = await ctx.deps.db.scalar(
        select(ProviderModel).where(
            ProviderModel.usuario_id == int(ctx.deps.user_id)
            if ctx.deps.user_id.isdigit()
            else ProviderModel.usuario_id == -1
        )
    )
    if row is None:
        return {"error": "No tenés un perfil de prestador registrado aún."}
    return {
        "id": row.id,
        "nombre": row.nombre,
        "rubros": json.loads(row.rubros) if row.rubros else [],
        "zona": row.zona,
        "plan": row.plan,
        "badge_verificado": row.badge_activo,
        "activo": row.activo,
        "disponibilidad": row.disponibilidad,
        "experiencia": row.experiencia,
        "facturacion": row.facturacion,
    }
