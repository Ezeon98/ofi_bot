"""Canonical rubro helpers shared by search entry points."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata

CANONICAL_RUBROS: list[str] = [
    "Albañil", "Maestro Mayor de Obras", "Techista",
    "Colocador de Durlock", "Colocador de Cerámicos", "Yesero",
    "Pintor", "Impermeabilización", "Hormigón y Contrapisos",
    "Constructor de Piscinas", "Electricista",
    "Automatización y domótica", "Porteros eléctricos", "Plomero",
    "Gasista", "Destapaciones",
    "Instalación de calefones", "Instalación de termotanques",
    "Instalación de bombas de agua", "Reparación de pérdidas",
    "Técnico en aire acondicionado", "Refrigeración comercial",
    "Calefacción", "Instalación de estufas", "Mantenimiento HVAC",
    "Carpintero", "Carpintero de obra", "Fabricación de muebles",
    "Restauración de muebles", "Armado de muebles", "Herrero",
    "Soldador", "Rejas y portones", "Estructuras metálicas",
    "Automatización de portones", "Mantenimiento integral",
    "Reparaciones generales", "Colocación de cortinas",
    "Colocación de cuadros y estantes", "Cerrajero",
    "Instalador de alarmas", "Instalador de cámaras",
    "Control de acceso", "Cercos eléctricos", "Jardinero",
    "Poda de árboles", "Paisajismo", "Sistemas de riego",
    "Limpieza de terrenos", "Parquización",
    "Limpieza domiciliaria", "Limpieza de oficinas",
    "Limpieza de finales de obra", "Limpieza de vidrios",
    "Limpieza de tapizados", "Desinfección", "Mudanzas", "Fletes",
    "Mini fletes", "Transporte de materiales",
    "Transporte de muebles", "Mecánico", "Electricidad automotor",
    "Gomería", "Chapista", "Pintura automotor", "Auxilio mecánico",
    "Técnico de PC", "Redes e Internet", "Reparación de celulares",
    "Instalación de software", "Soporte IT",
    "Instalación de impresoras",
    "Instalación de TV satelital",
    "Diseñador gráfico", "Fotógrafo", "Videógrafo",
    "Community Manager", "Diseñador web", "Contador", "Abogado",
    "Escribano", "Arquitecto", "Ingeniero", "Agrimensor",
    "Enfermero domiciliario", "Kinesiólogo", "Masajista",
    "Personal trainer", "Nutricionista", "Paseador de perros",
    "Peluquería canina", "Adiestrador", "Cuidador de mascotas",
    "Veterinario a domicilio", "DJ", "Sonido e iluminación",
    "Catering", "Mozo", "Decoración de eventos",
    "Fotografía de eventos", "Tornero", "Mecánico industrial",
    "Electromecánico", "Mantenimiento industrial",
    "Instrumentación industrial", "Alambrador", "Tractorista",
    "Mantenimiento rural", "Perforaciones",
    "Sistemas de riego agrícola", "Manicura", "Pedicura",
    "Nail Art", "Esculpidas en gel", "Esculpidas acrílicas",
    "Lashista", "Lifting de pestañas", "Perfilado de cejas",
    "Microblading", "Maquilladora", "Peinadora", "Peluquera",
    "Barbero", "Cosmetóloga", "Esteticista", "Depilación",
    "Masajes estéticos", "Niñera", "Cuidado de adultos mayores",
    "Cuidado de personas con discapacidad",
]

_RELATED_RUBRO_GROUPS: tuple[tuple[str, ...], ...] = (
    (
        "Electricista",
        "Automatización y domótica",
        "Instalador de cámaras",
        "Instalador de alarmas",
        "Control de acceso",
        "Porteros eléctricos",
    ),
    (
        "Técnico en aire acondicionado",
        "Refrigeración comercial",
        "Mantenimiento HVAC",
        "Calefacción",
        "Instalación de estufas",
    ),
    (
        "Plomero",
        "Gasista",
        "Instalación de calefones",
        "Instalación de termotanques",
        "Instalación de bombas de agua",
        "Reparación de pérdidas",
        "Destapaciones",
    ),
    (
        "Carpintero",
        "Carpintero de obra",
        "Fabricación de muebles",
        "Restauración de muebles",
        "Armado de muebles",
        "Herrero",
        "Soldador",
        "Rejas y portones",
        "Estructuras metálicas",
        "Automatización de portones",
        "Mantenimiento integral",
        "Colocación de cortinas",
        "Colocación de cuadros y estantes",
        "Cerrajero",
    ),
    (
        "Jardinero",
        "Poda de árboles",
        "Paisajismo",
        "Sistemas de riego",
        "Limpieza de terrenos",
        "Parquización",
    ),
    (
        "Limpieza domiciliaria",
        "Limpieza de oficinas",
        "Limpieza de finales de obra",
        "Limpieza de vidrios",
        "Limpieza de tapizados",
        "Desinfección",
    ),
    (
        "Mudanzas",
        "Fletes",
        "Mini fletes",
        "Transporte de materiales",
        "Transporte de muebles",
    ),
    (
        "Mecánico",
        "Electricidad automotor",
        "Gomería",
        "Chapista",
        "Pintura automotor",
        "Auxilio mecánico",
    ),
    (
        "Técnico de PC",
        "Redes e Internet",
        "Reparación de celulares",
        "Instalación de software",
        "Soporte IT",
        "Instalación de impresoras",
        "Instalación de TV satelital",
    ),
    (
        "Diseñador gráfico",
        "Fotógrafo",
        "Videógrafo",
        "Community Manager",
        "Diseñador web",
    ),
    (
        "Contador",
        "Abogado",
        "Escribano",
        "Arquitecto",
        "Ingeniero",
        "Agrimensor",
    ),
    (
        "Enfermero domiciliario",
        "Kinesiólogo",
        "Masajista",
        "Personal trainer",
        "Nutricionista",
        "Paseador de perros",
        "Peluquería canina",
        "Adiestrador",
        "Cuidador de mascotas",
        "Veterinario a domicilio",
    ),
    (
        "DJ",
        "Sonido e iluminación",
    ),
    (
        "Catering",
        "Mozo",
        "Decoración de eventos",
        "Fotografía de eventos",
    ),
    (
        "Tornero",
        "Mecánico industrial",
        "Electromecánico",
        "Mantenimiento industrial",
        "Instrumentación industrial",
    ),
    (
        "Alambrador",
        "Tractorista",
        "Mantenimiento rural",
        "Perforaciones",
        "Sistemas de riego agrícola",
    ),
    (
        "Manicura",
        "Pedicura",
        "Nail Art",
        "Esculpidas en gel",
        "Esculpidas acrílicas",
        "Lashista",
        "Lifting de pestañas",
        "Perfilado de cejas",
        "Microblading",
        "Maquilladora",
        "Peinadora",
        "Peluquera",
        "Barbero",
        "Cosmetóloga",
        "Esteticista",
        "Depilación",
        "Masajes estéticos",
    ),
    (
        "Niñera",
        "Cuidado de adultos mayores",
        "Cuidado de personas con discapacidad",
    ),
)

_RELATED_RUBROS_BY_CANONICAL: dict[str, tuple[str, ...]] = {
    rubro: group for group in _RELATED_RUBRO_GROUPS for rubro in group
}

_TOKEN_ALIASES = {
    "instalador": "instalacion",
    "instalar": "instalacion",
    "tecnica": "tecnico",
    "aires": "aire",
}
_TOKEN_STOPWORDS = {
    "a",
    "al",
    "con",
    "de",
    "del",
    "el",
    "en",
    "la",
    "las",
    "los",
    "para",
    "por",
    "un",
    "una",
    "y",
}


def resolve_canonical_rubro(rubro: str | None) -> str | None:
    """Return the closest canonical rubro when the input is near-canonical."""
    if not rubro:
        return rubro

    canonical_lower = {item.lower(): item for item in CANONICAL_RUBROS}
    if rubro.lower() in canonical_lower:
        return canonical_lower[rubro.lower()]

    normalized = _normalize_rubro_text(rubro)
    if not normalized:
        return rubro
    if "aire" in normalized and "acondicionado" in normalized:
        return "Técnico en aire acondicionado"

    best_match = rubro
    best_score = 0.0
    for canonical in CANONICAL_RUBROS:
        score = _score_rubro_match(normalized, canonical)
        if score > best_score:
            best_match = canonical
            best_score = score

    if best_score >= 0.55:
        return best_match
    return rubro


def related_canonical_rubros(rubro: str | None, limit: int = 4) -> list[str]:
    """Return a small ordered set of canonical rubros for fallback search."""
    canonical = resolve_canonical_rubro(rubro)
    if not canonical:
        return []

    group = _RELATED_RUBROS_BY_CANONICAL.get(canonical, (canonical,))
    ordered = [canonical]
    ordered.extend(item for item in group if item != canonical)
    return ordered[: max(1, limit)]


def _score_rubro_match(normalized_rubro: str, canonical: str) -> float:
    """Score how well a free-text rubro matches one canonical option."""
    normalized_canonical = _normalize_rubro_text(canonical)
    ratio = SequenceMatcher(
        None, normalized_rubro, normalized_canonical,
    ).ratio()

    rubro_tokens = set(_tokenize_rubro(normalized_rubro))
    canonical_tokens = set(_tokenize_rubro(normalized_canonical))
    overlap_base = min(len(rubro_tokens), len(canonical_tokens)) or 1
    overlap = len(rubro_tokens & canonical_tokens) / overlap_base
    contains_bonus = 1.0 if normalized_canonical in normalized_rubro else 0.0
    return (0.55 * overlap) + (0.35 * ratio) + (0.10 * contains_bonus)


def _tokenize_rubro(text: str) -> list[str]:
    """Normalize rubro text into comparable service tokens."""
    tokens: list[str] = []
    for raw_token in re.findall(r"[a-z0-9ñ]+", text):
        token = _TOKEN_ALIASES.get(raw_token, raw_token)
        if token in _TOKEN_STOPWORDS:
            continue
        if len(token) > 4 and token.endswith("es"):
            token = token[:-2]
        elif len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        tokens.append(token)
    return tokens


def _normalize_rubro_text(value: str) -> str:
    """Lowercase and strip accents to compare rubro labels safely."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", ascii_only.strip().lower())