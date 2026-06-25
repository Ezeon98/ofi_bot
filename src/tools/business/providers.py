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

from src.agents.dependencies import AgentDependencies
from src.infrastructure.database.models import (
    ProviderModel,
    ProviderTradeModel,
    TradeModel,
    UsuarioModel,
)
from src.utils.geocoding import geocode_text_location
from src.utils.agent_logger import AgentLogger

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
    limit: int = Field(default=3, ge=3, le=5)
    mensaje_contacto: str = Field(
        default="Hola, te contacto por ServiMatch para consultar sobre tus servicios.",
        description="Predefined message sent when the user taps 'Contactar'",
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

    if effective_params.rubro:
        rubro_terms = _build_rubro_search_terms(effective_params.rubro)
        logger.info(
            "PROVIDER_RUBRO user=%s rubro=%r rubro_terms=%s",
            user_id, effective_params.rubro, rubro_terms,
        )
        provider_ids_for_trade = (
            select(ProviderTradeModel.provider_id)
            .join(TradeModel, TradeModel.id == ProviderTradeModel.trade_id)
            .where(
                or_(
                    *[
                        clause
                        for rubro_term in rubro_terms
                        for clause in (
                            TradeModel.nombre.ilike(rubro_term),
                            TradeModel.slug.ilike(rubro_term),
                        )
                    ]
                )
            )
        )
        stmt = stmt.where(
            or_(
                ProviderModel.id.in_(provider_ids_for_trade),
                *[ProviderModel.rubros.ilike(rubro_term) for rubro_term in rubro_terms],
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
    logger.info(
        "PROVIDER_RAW user=%s raw_count=%d",
        user_id, len(rows),
    )

    # Extract ProviderModel instances and telefono from the joined result
    provider_rows = [r[0] for r in rows]
    telefono_map = {r[0].id: r[1] for r in rows}

    # (A) Batch-load trade names for all matched providers in one query
    provider_ids = [r.id for r in provider_rows]
    trade_names_by_provider = await _batch_provider_trade_names(ctx.deps.db, provider_ids)

    results = []
    for r in provider_rows:
        rubros = trade_names_by_provider.get(r.id) or (
            json.loads(r.rubros) if r.rubros else []
        )
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
        return {"error": "Ya existe un perfil de prestador para este usuario.", "id": existing.id}

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
    return {"id": provider.id, "nombre": provider.nombre, "estado": "pendiente_revision"}


async def actualizar_prestador(
    ctx: RunContext[AgentDependencies],
    params: ActualizarPrestadorInput,
) -> dict[str, Any]:
    """Update a single field of the current user's provider profile."""
    usuario_id = await _resolve_usuario_id(ctx.deps.db, ctx.deps.user_id)
    if usuario_id is None:
        return {"error": "No se encontró perfil de prestador para este usuario."}

    allowed_fields = {"disponibilidad", "experiencia", "barrio", "ciudad", "rubros"}
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
    usuario_id = await _resolve_usuario_id(ctx.deps.db, ctx.deps.user_id)
    if usuario_id is None:
        return {"error": "No tenés un perfil de prestador registrado aún."}

    row = await ctx.deps.db.scalar(
        select(ProviderModel).where(ProviderModel.usuario_id == usuario_id)
    )
    if row is None:
        return {"error": "No tenés un perfil de prestador registrado aún."}
    rubros = await _provider_trade_names(ctx.deps.db, row.id, row.rubros)
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


async def _batch_provider_trade_names(
    db: Any,
    provider_ids: list[int],
) -> dict[int, list[str]]:
    """Load trade names for multiple providers in a single query.

    Returns a dict mapping provider_id -> [trade_name, ...].
    Providers without trade links get an empty list.
    """
    if not provider_ids:
        return {}

    rows = await db.execute(
        select(ProviderTradeModel.provider_id, TradeModel.nombre)
        .join(TradeModel, TradeModel.id == ProviderTradeModel.trade_id)
        .where(ProviderTradeModel.provider_id.in_(provider_ids))
        .order_by(TradeModel.nombre.asc())
    )
    result: dict[int, list[str]] = {pid: [] for pid in provider_ids}
    for pid, name in rows:
        result.setdefault(pid, []).append(name)
    return result


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


async def _provider_trade_names(db: Any, provider_id: int, rubros_json: str) -> list[str]:
    """Return normalized trade names, falling back to the legacy rubros cache.

    Used for single-provider lookups (e.g. consultar_prestador).
    For batch lookups use _batch_provider_trade_names.
    """
    rows = await db.execute(
        select(TradeModel.nombre)
        .join(ProviderTradeModel, ProviderTradeModel.trade_id == TradeModel.id)
        .where(ProviderTradeModel.provider_id == provider_id)
        .order_by(TradeModel.nombre.asc())
    )
    trade_names = list(rows.scalars())
    if trade_names:
        return trade_names
    return json.loads(rubros_json) if rubros_json else []


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
    """Normalize location fields so oficio text does not leak into barrio/ciudad."""
    barrio, ciudad = _sanitize_location_fields(
        params.rubro,
        params.barrio,
        params.ciudad,
    )
    effective_limit = min(params.limit, 3)
    if (
        barrio == params.barrio
        and ciudad == params.ciudad
        and effective_limit == params.limit
    ):
        return params
    return params.model_copy(
        update={"barrio": barrio, "ciudad": ciudad, "limit": effective_limit}
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


def _build_rubro_search_terms(rubro: str) -> list[str]:
    """Build LIKE patterns that cover common oficio vs. rubro label variants.

    Uses synonym expansion so that e.g. "electricista" generates terms that
    also match "Electricidad" in the database.
    """
    return _expand_rubro_synonyms(rubro)


def _normalize_rubro_text(text: str) -> str:
    """Lowercase and strip accents so oficio comparisons are more tolerant."""
    normalized = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", without_marks.strip().lower())


# ── Trade/Ofício synonym map ─────────────────────────────────────────────────
# Maps common user queries to DB-compatible search stems so that, e.g.,
# "electricista" matches providers who registered with rubro "Electricidad".
_TRADE_SYNONYM_STEMS: dict[str, tuple[str, ...]] = {
    # electricidad / electricista
    "electric": ("electricid", "electricist", "electric"),
    # gas / gasista
    "gas": ("gasist", "gas"),
    # plomero / plomeria / plomera
    "plomer": ("plomer",),
    # cerrajero / cerrajeria
    "cerraj": ("cerrajer", "cerraj"),
    # albañil / albañileria
    "albañil": ("albañil",),
    "albanni": ("albannil",),
    # pintor / pintura
    "pintor": ("pint"),
    "pintur": ("pint"),
    # herrero / herreria
    "herr": ("herr",),
    # carpintero / carpinteria
    "carpinter": ("carpinter",),
    # jardinero / jardineria
    "jardin": ("jardin",),
    # fontanero / fontaneria
    "fontan": ("fontan",),
    # cocinero / cocina
    "cocin": ("cocin",),
    # profesor / profesorado
    "profesor": ("profesor",),
    # abogado / abogacia
    "abog": ("abog",),
    # contador / contaduria
    "contador": ("contador",),
    # medico / medicina
    "medic": ("medic",),
    # enfermero / enfermeria
    "enfermer": ("enfermer",),
    # veterinario / veterinaria
    "veterinari": ("veterinari",),
    # ingeniero / ingenieria
    "ingenier": ("ingenier",),
    # arquitecto / arquitectura
    "arquitect": ("arquitect",),
    # tecnico / tecnica
    "tecnic": ("tecnic",),
    # reparador / reparacion
    "repar": ("repar",),
    # limpieza / limpiador
    "limpi": ("limpi",),
    # cuidado / cuidador
    "cuidad": ("cuidad",),
    # mascota / paseador
    "mascot": ("mascot",),
    # seguridad / vigilador
    "segur": ("segur",),
    # profesor / maestro
    "profes": ("profes",),
    # traductor / traduccion
    "traductor": ("traductor",),
    "traduccion": ("traduccion",),
    # chofer / transporte
    "chofer": ("chofer",),
    "conduct": ("conduct",),
    "transport": ("transport",),
    # flete / mudanza
    "flet": ("flet",),
    "mudanz": ("mudanz",),
    "mudanza": ("mudanza",),
    # niñero / niñera / niñera
    "niñer": ("niñer",),
    "ninier": ("ninier",),
}


def _profession_stem(word: str) -> str | None:
    """Return a broad stem for oficio nouns.

    Handles common Spanish profession suffixes and also returns a SYNONYM
    expansion so that, e.g., "electricista" also generates "electricid" which
    matches the DB rubro "Electricidad".
    """
    if len(word) < 4:
        return None

    # Try to find a known synonym stem by matching the first N characters
    for stem, expansions in _TRADE_SYNONYM_STEMS.items():
        if word.startswith(stem):
            return expansions[0]
        # Also check if any expansion starts with the word
        for exp in expansions:
            if exp.startswith(word) or word.startswith(exp):
                return exp

    # Suffix-based fallback
    if word.endswith(("ero", "era", "eria")):
        # plomero -> plomer, plomeria -> plomer
        return word[:-1] if not word.endswith("eria") else word[:-2]
    if word.endswith(("ista", "ista")):
        # electricista -> electricist (also handles gasista, etc.)
        return word[:-4] if len(word) > 5 else None
    if word.endswith(("dor", "dora", "tor", "tora")):
        # reparador -> reparador
        # removedor -> removedor
        return word[:-2] if len(word) > 5 else None
    if word.endswith(("nte", "nte")):
        # estudiante, auxiliante -> estudiant, auxiliant
        return word[:-2] if len(word) > 5 else None

    return None


def _expand_rubro_synonyms(rubro: str) -> list[str]:
    """Generate all known synonym variants for a given rubro search term.

    E.g. "electricista" -> ["%electricista%", "%electricist%", "%electricidad%", "%electric%"]
    """
    normalized = _normalize_rubro_text(rubro)
    expanded: set[str] = set()

    # 1. The original normalized text
    if normalized:
        expanded.add(normalized)

    # 2. Each individual token
    for token in normalized.split():
        expanded.add(token)
        stem = _profession_stem(token)
        if stem and stem != token:
            expanded.add(stem)

        # 3. Synonym expansions from the trade map
        for key, expansions in _TRADE_SYNONYM_STEMS.items():
            if token.startswith(key) or (stem and stem.startswith(key)):
                for exp in expansions:
                    expanded.add(exp)
            # Reverse: also check if the key starts with the token
            if key.startswith(token):
                for exp in expansions:
                    expanded.add(exp)

    return [f"%{t}%" for t in expanded if t]


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