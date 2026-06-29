"""Business tools: provider operations.

Each function represents a BUSINESS ACTION, not a generic DB operation.
The LLM calls these by name; names must be self-documenting.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from sqlalchemy import or_, select, update

from src.agents.dependencies import AgentDependencies, db_access_lock
from src.infrastructure.database.models import (
    ProviderModel,
    UsuarioModel,
)
from src.utils.geocoding import geocode_text_location
from src.utils.agent_logger import AgentLogger
from src.utils.rubros import related_canonical_rubros, resolve_canonical_rubro

logger = logging.getLogger(__name__)

# Module-level AgentLogger, enabled by default. Its enabled flag is toggled
# when it's bound to a turn_id via AgentDependencies (not needed here since
# we log with the raw logger name "agent").
_alog = AgentLogger(enabled=True)


# ── Input schemas ────────────────────────────────────────────────────────────


class BuscarPrestadoresInput(BaseModel):
    rubro: str = Field(description="Service category, e.g. 'plomero', 'electricista'")
    barrio: str | None = Field(default=None, description="Neighborhood or district name")
    ciudad: str | None = Field(default=None, description="City or locality name")
    lat: float | None = Field(default=None, description="Optional user latitude for ranking")
    lon: float | None = Field(default=None, description="Optional user longitude for ranking")
    solo_verificados: bool = Field(default=False)
    limit: int = Field(default=3, ge=3, le=15)
    mensaje_contacto: str = Field(
        default="Hola, te contacto por ServiMatch para consultar sobre tus servicios.",
        description="Predefined message sent when the user taps 'Contactar'",
    )


class RubrosRelacionadosInput(BaseModel):
    rubro: str = Field(description="Canonical or near-canonical requested trade")
    limit: int = Field(default=4, ge=1, le=6)


class ResolverUbicacionInput(BaseModel):
    ubicacion: str = Field(
        description="Neighborhood, locality or city extracted from the user's current message"
    )


class CrearPrestadorInput(BaseModel):
    nombre: str
    rubros: list[str] = Field(min_length=1, max_length=5)
    barrio: str | None = Field(default=None, description="Neighborhood or district")
    ciudad: str | None = Field(default=None, description="City or locality")
    disponibilidad: str | None = None
    experiencia: str | None = Field(default=None, max_length=500)
    facturacion: str = Field(default="no_factura")


class ActualizarPrestadorInput(BaseModel):
    field: str = Field(description="Field to update: disponibilidad, experiencia, barrio, ciudad, rubros")
    value: str = Field(description="New value (rubros as JSON array string)")


class ConsultarPrestadorInput(BaseModel):
    pass  # uses user_id from deps


# ── Tool implementations ─────────────────────────────────────────────────────


async def buscar_prestadores(
    ctx: RunContext[AgentDependencies],
    params: BuscarPrestadoresInput,
) -> list[dict[str, Any]]:
    """Find active service providers matching a rubro and optional location.

    Returns up to `limit` results, verified providers first.
    """
    effective_params = _sanitize_search_params(params)

    # ── Log search entry ──────────────────────────────────────────────
    user_id = ctx.deps.user_id if hasattr(ctx.deps, "user_id") else "?"
    logger.info(
        "PROVIDER_SEARCH user=%s rubro=%r barrio=%r ciudad=%r lat=%r lon=%r solo_ver=%d limit=%d",
        user_id,
        effective_params.rubro,
        effective_params.barrio,
        effective_params.ciudad,
        effective_params.lat,
        effective_params.lon,
        effective_params.solo_verificados,
        effective_params.limit,
    )

    origin_lat, origin_lon = await _resolve_search_origin(
        ctx.deps.current_message_metadata,
        effective_params,
    )

    logger.info(
        "PROVIDER_ORIGIN user=%s origin_lat=%r origin_lon=%r",
        user_id, origin_lat, origin_lon,
    )

    async with db_access_lock(ctx.deps):
        stmt = (
            select(ProviderModel, UsuarioModel.telefono)
            .join(UsuarioModel, UsuarioModel.id == ProviderModel.usuario_id)
            .where(ProviderModel.activo == True)  # noqa: E712
        )

        if params.solo_verificados:
            stmt = stmt.where(ProviderModel.badge_activo == True)  # noqa: E712

        # Apply text-based location filter using barrio/ciudad
        use_text_location_filter = _should_apply_text_location_filter(
            effective_params.barrio,
            effective_params.ciudad,
            origin_lat,
            origin_lon,
        )
        logger.info(
            "PROVIDER_FILTER user=%s use_text_location=%s barrio=%s ciudad=%s",
            user_id,
            use_text_location_filter,
            effective_params.barrio,
            effective_params.ciudad,
        )

        if use_text_location_filter:
            location_filters = []
            if effective_params.barrio:
                barrio_term = f"%{effective_params.barrio}%"
                location_filters.append(ProviderModel.barrio.ilike(barrio_term))
                location_filters.append(ProviderModel.ciudad.ilike(barrio_term))
            if effective_params.ciudad:
                ciudad_term = f"%{effective_params.ciudad}%"
                location_filters.append(ProviderModel.barrio.ilike(ciudad_term))
                location_filters.append(ProviderModel.ciudad.ilike(ciudad_term))
            if location_filters:
                stmt = stmt.where(or_(*location_filters))

        rubro_filters = _search_rubros_for(effective_params.rubro)
        if rubro_filters:
            logger.info(
                "PROVIDER_RUBRO user=%s rubro=%r rubro_pattern=%r related=%r",
                user_id,
                effective_params.rubro,
                _legacy_rubro_json_pattern(effective_params.rubro),
                _related_rubro_suggestions(effective_params.rubro),
            )
            stmt = stmt.where(
                ProviderModel.rubros.like(
                    _legacy_rubro_json_pattern(rubro_filters[0])
                )
            )

        stmt = (
            stmt.order_by(
                ProviderModel.badge_activo.desc(),
                ProviderModel.plan.desc(),
            )
            .limit(max(effective_params.limit * 3, 15))
        )

        rows = list(await ctx.deps.db.execute(stmt))
        provider_rows = [r[0] for r in rows]
        telefono_map = {r[0].id: r[1] for r in rows}
    logger.info(
        "PROVIDER_RAW user=%s raw_count=%d",
        user_id, len(rows),
    )

    results = []
    for r in provider_rows:
        rubros = _parse_rubros_json(r.rubros)
        results.append(
            {
                "nombre": r.nombre,
                "rubros": rubros,
                "ciudad": r.ciudad,
                "barrio": r.barrio,
                "lat": r.lat,
                "lon": r.lon,
                "telefono": telefono_map.get(r.id),
                "disponibilidad": r.disponibilidad,
                "badge_verificado": r.badge_activo,
                "facturacion": r.facturacion,
                "distance_km": _distance_km(origin_lat, origin_lon, r.lat, r.lon),
            }
        )
    results.sort(
        key=lambda item: (
            0 if item["badge_verificado"] else 1,
            item["distance_km"] if item["distance_km"] is not None else float("inf"),
        )
    )
    final = results[: effective_params.limit]
    logger.info(
        "PROVIDER_RESULT user=%s final_count=%d names=%r",
        user_id, len(final), [r["nombre"] for r in final],
    )
    return final


async def crear_prestador(
    ctx: RunContext[AgentDependencies],
    params: CrearPrestadorInput,
) -> dict[str, Any]:
    """Register a new service provider profile for the current user.

    Returns the created provider summary.
    """
    from sqlalchemy import select as sa_select

    async with db_access_lock(ctx.deps):
        usuario_id = await _resolve_usuario_id(ctx.deps.db, ctx.deps.user_id)
        if usuario_id is None:
            return {"error": "No se encontró el usuario para crear el perfil de prestador."}

        # Prevent duplicates
        existing = await ctx.deps.db.scalar(
            sa_select(ProviderModel).where(
                ProviderModel.usuario_id == usuario_id
            )
        )
        if existing:
            return {
                "error": "Ya existe un perfil de prestador para este usuario.",
                "id": existing.id,
            }

        provider = ProviderModel(
            usuario_id=usuario_id,
            nombre=params.nombre,
            rubros=json.dumps(params.rubros, ensure_ascii=False),
            ciudad=params.ciudad,
            barrio=params.barrio,
            disponibilidad=params.disponibilidad,
            experiencia=params.experiencia,
            facturacion=params.facturacion,
            plan="free",
            activo=False,  # needs manual review
        )
        ctx.deps.db.add(provider)
        await ctx.deps.db.flush()
        return {
            "id": provider.id,
            "nombre": provider.nombre,
            "estado": "pendiente_revision",
        }


async def actualizar_prestador(
    ctx: RunContext[AgentDependencies],
    params: ActualizarPrestadorInput,
) -> dict[str, Any]:
    """Update a single field of the current user's provider profile."""
    allowed_fields = {"disponibilidad", "experiencia", "barrio", "ciudad", "rubros"}
    if params.field not in allowed_fields:
        return {"error": f"Campo no permitido: {params.field}"}

    value: Any = params.value
    if params.field == "rubros":
        try:
            value = json.dumps(json.loads(params.value), ensure_ascii=False)
        except json.JSONDecodeError:
            return {"error": "El campo rubros debe ser un JSON array de strings."}

    async with db_access_lock(ctx.deps):
        usuario_id = await _resolve_usuario_id(ctx.deps.db, ctx.deps.user_id)
        if usuario_id is None:
            return {"error": "No se encontró perfil de prestador para este usuario."}

        result = await ctx.deps.db.execute(
            update(ProviderModel)
            .where(ProviderModel.usuario_id == usuario_id)
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
    async with db_access_lock(ctx.deps):
        usuario_id = await _resolve_usuario_id(ctx.deps.db, ctx.deps.user_id)
        if usuario_id is None:
            return {"error": "No tenés un perfil de prestador registrado aún."}

        row = await ctx.deps.db.scalar(
            select(ProviderModel).where(ProviderModel.usuario_id == usuario_id)
        )
        if row is None:
            return {"error": "No tenés un perfil de prestador registrado aún."}
        rubros = _parse_rubros_json(row.rubros)
    return {
        "id": row.id,
        "nombre": row.nombre,
        "rubros": rubros,
        "ciudad": row.ciudad,
        "barrio": row.barrio,
        "lat": row.lat,
        "lon": row.lon,
        "plan": row.plan,
        "badge_verificado": row.badge_activo,
        "activo": row.activo,
        "disponibilidad": row.disponibilidad,
        "experiencia": row.experiencia,
        "facturacion": row.facturacion,
    }


async def buscar_rubros_relacionados(
    _ctx: RunContext[AgentDependencies],
    params: RubrosRelacionadosInput,
) -> dict[str, Any]:
    """Return nearby canonical rubros so the AI can broaden a sparse search."""
    canonical = resolve_canonical_rubro(params.rubro) or params.rubro
    return {
        "rubro": canonical,
        "alternativas": _related_rubro_suggestions(canonical, limit=params.limit),
    }


async def resolver_ubicacion(
    _ctx: RunContext[AgentDependencies],
    params: ResolverUbicacionInput,
) -> dict[str, Any]:
    """Resolve a textual zone into normalized city/neighborhood fields."""
    query = re.sub(r"\s+", " ", params.ubicacion.strip())
    if not query:
        return {
            "resolved": False,
            "query": params.ubicacion,
            "barrio": None,
            "ciudad": None,
            "lat": None,
            "lon": None,
            "display_name": None,
        }

    geocoded = await geocode_text_location(query)
    return {
        "resolved": bool(geocoded),
        "query": query,
        "barrio": geocoded.get("barrio"),
        "ciudad": geocoded.get("ciudad"),
        "lat": geocoded.get("lat"),
        "lon": geocoded.get("lon"),
        "display_name": geocoded.get("display_name"),
    }


def build_provider_search_report(
    params: BuscarPrestadoresInput,
    providers: list[dict[str, Any]],
    *,
    status: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Return the structured search report consumed by the router agent."""
    effective_params = _sanitize_search_params(params)
    related_rubros = _related_rubro_suggestions(effective_params.rubro)
    provider_count = len(providers)
    effective_status = status or ("ok" if provider_count else "no_results")
    return {
        "status": effective_status,
        "message": message,
        "requested_rubro": params.rubro,
        "resolved_rubro": effective_params.rubro,
        "related_rubros": related_rubros,
        "location": {
            "barrio": effective_params.barrio,
            "ciudad": effective_params.ciudad,
            "lat": effective_params.lat,
            "lon": effective_params.lon,
        },
        "provider_count": provider_count,
        "requested_limit": effective_params.limit,
        "sufficient_results": provider_count >= effective_params.limit,
        "providers": providers,
    }


async def _resolve_usuario_id(db: Any, raw_user_id: str) -> int | None:
    """Resolve the DB user id from the bot user identifier.

    The bot layer passes the sender phone number as user_id. Some internal call
    sites may still pass a numeric UsuarioModel.id, so we fall back to that only
    if there is no user row with that phone.
    """
    usuario_id = await db.scalar(
        select(UsuarioModel.id).where(UsuarioModel.telefono == raw_user_id)
    )
    if usuario_id is not None:
        return int(usuario_id)
    if raw_user_id.isdigit():
        return int(raw_user_id)
    return None


def _parse_rubros_json(rubros_json: str | None) -> list[str]:
    """Parse the provider rubros JSON array, tolerating invalid legacy values."""
    if not rubros_json:
        return []
    try:
        raw = json.loads(rubros_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item]


def _search_rubros_for(rubro: str | None) -> list[str]:
    """Build the exact rubro filter used for a single search attempt."""
    canonical = resolve_canonical_rubro(rubro) or rubro
    return [canonical] if canonical else []


def _related_rubro_suggestions(rubro: str | None, limit: int = 4) -> list[str]:
    """Return only alternative rubros, excluding the requested primary one."""
    canonical = resolve_canonical_rubro(rubro) or rubro
    if not canonical:
        return []
    return [
        item
        for item in related_canonical_rubros(canonical, limit=limit + 1)
        if item != canonical
    ][:limit]


async def _resolve_search_origin(
    metadata: dict[str, Any] | None,
    params: BuscarPrestadoresInput,
) -> tuple[float | None, float | None]:
    """Pick the best available search origin for distance ranking.

    Builds a location label from {barrio, ciudad} for geocoding if needed.
    """
    if params.lat is not None and params.lon is not None:
        return params.lat, params.lon

    location_label = _build_location_label(params.barrio, params.ciudad)
    if not metadata and location_label:
        geocoded = await geocode_text_location(location_label)
        latitude = geocoded.get("lat")
        longitude = geocoded.get("lon")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return float(latitude), float(longitude)
        return None, None

    if metadata:
        latitude = metadata.get("latitude")
        longitude = metadata.get("longitude")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return float(latitude), float(longitude)

    if location_label:
        geocoded = await geocode_text_location(location_label)
        latitude = geocoded.get("lat")
        longitude = geocoded.get("lon")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return float(latitude), float(longitude)

    return None, None


def _build_location_label(barrio: str | None, ciudad: str | None) -> str | None:
    """Build a human-readable location label from barrio and ciudad."""
    parts = [part for part in [barrio, ciudad] if isinstance(part, str) and part]
    return ", ".join(parts) if parts else None


def _sanitize_search_params(
    params: BuscarPrestadoresInput,
) -> BuscarPrestadoresInput:
    """Normalize rubro and location fields before hitting the provider query."""
    rubro = resolve_canonical_rubro(params.rubro) or params.rubro
    barrio, ciudad = _sanitize_location_fields(
        rubro,
        params.barrio,
        params.ciudad,
    )
    effective_limit = min(params.limit, 15)
    if (
        rubro == params.rubro
        and
        barrio == params.barrio
        and ciudad == params.ciudad
        and effective_limit == params.limit
    ):
        return params
    return params.model_copy(
        update={
            "rubro": rubro,
            "barrio": barrio,
            "ciudad": ciudad,
            "limit": effective_limit,
        }
    )


def _sanitize_location_fields(
    rubro: str,
    barrio: str | None,
    ciudad: str | None,
) -> tuple[str | None, str | None]:
    """Strip repeated oficio text from free-text location fields."""
    normalized_rubro = _normalize_rubro_text(rubro)
    return (
        _sanitize_location_field(normalized_rubro, barrio),
        _sanitize_location_field(normalized_rubro, ciudad),
    )


def _sanitize_location_field(
    normalized_rubro: str,
    value: str | None,
) -> str | None:
    """Keep only the location fragment when the field includes a full user query."""
    if not value:
        return value

    compact = re.sub(r"\s+", " ", value.strip())
    if not compact:
        return None

    normalized_value = _normalize_rubro_text(compact)
    if not normalized_value:
        return compact

    location_fragment = _extract_location_fragment(normalized_value)
    if location_fragment is not None:
        return location_fragment.title()

    if normalized_rubro and normalized_value.startswith(normalized_rubro):
        remainder = normalized_value[len(normalized_rubro):].strip(" ,.-")
        if remainder:
            return remainder.title()

    return compact


def _extract_location_fragment(text: str) -> str | None:
    """Return the suffix after common Spanish location connectors."""
    match = re.search(r"\b(?:en|por|para|cerca de|zona de|barrio de|localidad de)\b\s+(.+)", text)
    if match is None:
        return None
    fragment = match.group(1).strip(" .,!?:;")
    return fragment or None


def _should_apply_text_location_filter(
    barrio: str | None,
    ciudad: str | None,
    origin_lat: float | None,
    origin_lon: float | None,
) -> bool:
    """Use textual location filtering only when no coordinate origin is available."""
    return bool(barrio or ciudad) and origin_lat is None and origin_lon is None


def _legacy_rubro_json_pattern(rubro: str) -> str:
    """Build a legacy JSON-array pattern for an exact rubro element match."""
    return f"%{json.dumps(rubro, ensure_ascii=False)}%"


def _normalize_rubro_text(text: str) -> str:
    """Lowercase and strip accents so oficio comparisons are more tolerant."""
    normalized = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", without_marks.strip().lower())


def _distance_km(
    origin_lat: float | None,
    origin_lon: float | None,
    target_lat: float | None,
    target_lon: float | None,
) -> float | None:
    """Compute the distance between two coordinates when both are available."""
    if None in {origin_lat, origin_lon, target_lat, target_lon}:
        return None

    earth_radius_km = 6371.0
    lat1 = math.radians(float(origin_lat))
    lon1 = math.radians(float(origin_lon))
    lat2 = math.radians(float(target_lat))
    lon2 = math.radians(float(target_lon))
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return earth_radius_km * 2 * math.asin(math.sqrt(haversine))