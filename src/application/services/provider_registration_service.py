"""Provider registration service.

Handles the guided onboarding flow for users who want to register as
service providers without relying on the LLM for every turn.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import unicodedata
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from src.agents.dependencies import AgentDependencies
from src.agents.models.response import AgentResponse, Intent
from src.infrastructure.database.models import ProviderModel, UsuarioModel
from src.infrastructure.database.repositories.estado import EstadoRepository
from src.memory.service import MemoryService
from src.tools.business.providers import CrearPrestadorInput, crear_prestador
from src.utils.geocoding import geocode_text_location
from src.utils.rubros import CANONICAL_RUBROS, resolve_canonical_rubro

logger = logging.getLogger(__name__)

_RUBROS_FOR_PROMPT = ", ".join(CANONICAL_RUBROS)
_AI_TRADES_SYSTEM = (
    "Sos un asistente que clasifica oficios y servicios ofrecidos por prestadores en Argentina.\n"
    "Dado un mensaje del prestador, devolvé ÚNICAMENTE un JSON con este formato:\n"
    '  {"rubros": ["Rubro canónico 1", "Rubro canónico 2"]}.\n'
    "Cada elemento de rubros debe salir de esta lista exacta: "
    + _RUBROS_FOR_PROMPT
    + ".\n"
    "Si el mensaje describe varios servicios, devolvé hasta 5 rubros canónicos.\n"
    "Si no coincide literal pero describe un oficio real, elegí el rubro canónico más cercano.\n"
    'Si el mensaje no describe servicios concretos, devolvé {"rubros": []}.\n'
    "No incluyas texto fuera del JSON."
)
_AI_DNI_SYSTEM = (
    "Sos un asistente que analiza imágenes de documentos argentinos.\n"
    "Primero determiná si la imagen corresponde al frente de un DNI argentino.\n"
    "Respondé ÚNICAMENTE un JSON con este formato:\n"
    '  {"document_type": "dni_frente"|"otro"|"desconocido", '
    '"is_dni": true|false, "full_name": "Nombre Apellido"|null, '
    '"dni": "12345678"|null, "birth_date": "YYYY-MM-DD"|null}.\n'
    "Reglas:\n"
    "- Si no estás seguro de que sea un DNI, devolvé is_dni=false.\n"
    "- Si el documento no es un DNI o la imagen es ilegible, devolvé null en los datos.\n"
    "- El DNI debe tener exactamente 8 dígitos.\n"
    "- birth_date debe salir en formato YYYY-MM-DD si se puede leer.\n"
    "- No agregues texto fuera del JSON."
)

REGISTRATION_STATE_NAME = "provider_registration"
OFFER_SERVICES_BUTTON_ID = "post_terms_offer_services"
REGISTRATION_START_PATTERNS = (
    "quiero registrarme como proveedor",
    "quiero registrarme como prestador",
    "me quiero registrar como proveedor",
    "me quiero registrar como prestador",
    "quiero darme de alta como proveedor",
    "quiero darme de alta como prestador",
    "quiero ofrecer mis servicios",
    "quiero ofrecer servicios",
    "quiero ser proveedor",
    "quiero ser prestador",
    "quiero publicar mis servicios",
)
TRADE_PREFIXES = (
    "hago ",
    "me dedico a ",
    "trabajo de ",
    "ofrezco ",
    "se hacer ",
    "sé hacer ",
    "soy ",
)
ZONE_PREFIXES = (
    "estoy en ",
    "soy de ",
    "trabajo en ",
    "mi zona es ",
    "mi barrio es ",
    "vivo en ",
)
DNI_CONFIRM_YES = {"si", "sí"}
DNI_CONFIRM_NO = {"no"}


class ProviderRegistrationService:
    """Encapsulate the guided provider-registration flow."""

    def __init__(
        self,
        *,
        memory_config: Any,
        agent_logger: Any | None = None,
        openai_client: Any | None = None,
        openai_model: str = "gpt-4o-mini",
    ) -> None:
        self._memory_config = memory_config
        self._alog = agent_logger
        self._openai = openai_client
        self._openai_model = openai_model

    async def maybe_handle_registration(
        self,
        *,
        user_id: str,
        message: str,
        metadata: dict[str, Any] | None,
        deps: AgentDependencies,
        memory_service: MemoryService,
        turn_id: str = "",
    ) -> AgentResponse | None:
        """Handle provider-registration turns without invoking the LLM."""
        state_repo = EstadoRepository(deps.db)
        raw_state = await state_repo.get(user_id)
        state = raw_state if raw_state.get("estado") == REGISTRATION_STATE_NAME else {}
        paso = state.get("paso")

        self._log(
            turn_id,
            "provider_registration.state",
            paso=paso,
            state_keys=list(state.keys()),
        )

        if not paso:
            if not self._is_registration_start(message, metadata):
                return None
            if await self._provider_exists(deps.db, user_id):
                return AgentResponse(
                    intent=Intent.REGISTRAR_PRESTADOR,
                    message=(
                        "Ya tenés un perfil de prestador creado. "
                        "Si querés, te ayudo a actualizarlo."
                    ),
                    confidence=1.0,
                    requires_action=False,
                )
            await self._save_state(state_repo, user_id, paso="awaiting_dni")
            return self._build_dni_request_response()

        if paso == "awaiting_dni":
            return await self._handle_dni_step(
                state_repo=state_repo,
                user_id=user_id,
                metadata=metadata,
            )

        if paso == "awaiting_dni_confirmation":
            return await self._handle_dni_confirmation_step(
                state_repo=state_repo,
                user_id=user_id,
                message=message,
                metadata=metadata,
                state=state,
            )

        if paso == "awaiting_trades":
            rubros = await self._classify_trades(message)
            if not rubros:
                return self._build_trades_request_response(retry=True)
            await self._save_state(
                state_repo,
                user_id,
                paso="awaiting_zone",
                nombre=state.get("nombre"),
                edad=state.get("edad"),
                dni=state.get("dni"),
                fecha_nacimiento=state.get("fecha_nacimiento"),
                rubros=rubros,
            )
            return self._build_zone_request_response(rubros)

        if paso == "awaiting_zone":
            location = await self._resolve_location(message, metadata)
            if location is None:
                return self._build_zone_request_response(
                    state.get("rubros") or [],
                    retry=True,
                )
            return await self._complete_registration(
                state_repo=state_repo,
                user_id=user_id,
                deps=deps,
                memory_service=memory_service,
                state=state,
                location=location,
            )

        return None

    async def _complete_registration(
        self,
        *,
        state_repo: EstadoRepository,
        user_id: str,
        deps: AgentDependencies,
        memory_service: MemoryService,
        state: dict[str, Any],
        location: dict[str, Any],
    ) -> AgentResponse:
        """Persist the provider profile after all onboarding fields are present."""
        rubros = [str(item) for item in state.get("rubros") or [] if item]
        params = CrearPrestadorInput(
            nombre=str(state.get("nombre") or "Prestador"),
            dni=str(state.get("dni") or "") or None,
            edad=state.get("edad"),
            rubros=rubros or ["Servicios generales"],
            barrio=location.get("barrio"),
            ciudad=location.get("ciudad"),
        )
        result = await crear_prestador(SimpleNamespace(deps=deps), params)
        await state_repo.delete(user_id)

        entities = {
            "nombre": state.get("nombre"),
            "dni": state.get("dni"),
            "edad": state.get("edad"),
            "fecha_nacimiento": state.get("fecha_nacimiento"),
            "rubros": rubros,
            "barrio": location.get("barrio"),
            "ciudad": location.get("ciudad"),
        }
        if "error" in result:
            return AgentResponse(
                intent=Intent.REGISTRAR_PRESTADOR,
                message=(
                    "Ya tenés un perfil de prestador cargado. "
                    "Si querés, te ayudo a actualizarlo."
                ),
                confidence=1.0,
                entities=entities,
                requires_action=False,
            )

        if self._memory_config.enabled and state.get("edad") is not None:
            await memory_service.upsert_memory(
                deps.usuario_id,
                "provider_registration_age",
                str(state["edad"]),
                0.8,
            )

        zona = self._location_label(location) or "tu zona"
        rubros_str = ", ".join(rubros)
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=(
                f"Listo, ya inicié tu registro como prestador con {rubros_str} "
                f"en {zona}. Ahora queda pendiente de revisión para activarlo."
            ),
            confidence=1.0,
            entities=entities,
            requires_action=True,
            metadata={
                "provider_registration": {
                    "provider_id": result.get("id"),
                    "estado": result.get("estado"),
                    "dni": state.get("dni"),
                    "edad": state.get("edad"),
                }
            },
        )

    async def _handle_dni_step(
        self,
        *,
        state_repo: EstadoRepository,
        user_id: str,
        metadata: dict[str, Any] | None,
    ) -> AgentResponse:
        """Analyze the inbound DNI image and move the flow to confirmation."""
        if not self._metadata_has_image(metadata):
            return self._build_dni_request_response(retry=True)

        extracted = await self._extract_dni_data(metadata)
        if extracted is None:
            return self._build_dni_processing_error_response()

        if not extracted["is_dni"]:
            return self._build_dni_request_response(retry=True, invalid_document=True)

        if (
            extracted["nombre"] is None
            or extracted["dni"] is None
            or extracted["fecha_nacimiento"] is None
            or extracted["edad"] is None
        ):
            return self._build_dni_request_response(retry=True, invalid_document=True)

        if extracted["edad"] < 18:
            await state_repo.delete(user_id)
            return self._build_underage_response()

        await self._save_state(
            state_repo,
            user_id,
            paso="awaiting_dni_confirmation",
            nombre=extracted["nombre"],
            edad=extracted["edad"],
            dni=extracted["dni"],
            fecha_nacimiento=extracted["fecha_nacimiento"],
        )
        return self._build_dni_confirmation_response(extracted)

    async def _handle_dni_confirmation_step(
        self,
        *,
        state_repo: EstadoRepository,
        user_id: str,
        message: str,
        metadata: dict[str, Any] | None,
        state: dict[str, Any],
    ) -> AgentResponse:
        """Accept or reject the detected DNI fields before continuing."""
        if self._metadata_has_image(metadata):
            return await self._handle_dni_step(
                state_repo=state_repo,
                user_id=user_id,
                metadata=metadata,
            )

        confirmation = self._extract_confirmation(message)
        if confirmation is True:
            await self._save_state(
                state_repo,
                user_id,
                paso="awaiting_trades",
                nombre=state.get("nombre"),
                edad=state.get("edad"),
                dni=state.get("dni"),
                fecha_nacimiento=state.get("fecha_nacimiento"),
            )
            return self._build_trades_request_response()

        if confirmation is False:
            await self._save_state(state_repo, user_id, paso="awaiting_dni")
            return self._build_dni_request_response(retry=True)

        return self._build_dni_confirmation_retry_response()

    async def _classify_trades(self, message: str) -> list[str]:
        """Extract canonical provider rubros from the user's free-text reply."""
        raw_message = self._strip_prefixes(message, TRADE_PREFIXES)
        if not raw_message.strip():
            return []
        ai_rubros = await self._ai_classify_trades(raw_message)
        if ai_rubros:
            return ai_rubros
        return self._fallback_trade_labels(raw_message)

    async def _ai_classify_trades(self, message: str) -> list[str]:
        """Classify provider services into canonical rubros using the LLM."""
        if self._openai is None:
            return []

        try:
            resp = await self._openai.chat.completions.create(
                model=self._openai_model,
                messages=[
                    {"role": "system", "content": _AI_TRADES_SYSTEM},
                    {"role": "user", "content": message},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=128,
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            rubros = data.get("rubros")
            if not isinstance(rubros, list):
                return []
            return self._canonicalize_trade_labels(rubros)
        except Exception:
            logger.exception("AI trade classification failed, falling back to heuristics")
            return []

    async def _resolve_location(
        self,
        message: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Resolve the provider zone from shared location metadata or free text."""
        current_location = self._location_from_metadata(metadata)
        if current_location is not None:
            return current_location

        zone = self._extract_zone(message)
        if zone is None:
            return None

        geocoded = await geocode_text_location(zone)
        if geocoded:
            return {
                "lat": geocoded.get("lat"),
                "lon": geocoded.get("lon"),
                "ciudad": geocoded.get("ciudad"),
                "barrio": geocoded.get("barrio") or zone,
            }
        return {
            "lat": None,
            "lon": None,
            "ciudad": None,
            "barrio": zone,
        }

    async def _extract_dni_data(
        self,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Run a vision-capable LLM over the DNI image and normalize the fields."""
        image_path = self._image_path_from_metadata(metadata)
        if image_path is None or self._openai is None:
            return None

        mime_type = self._image_mime_type(metadata)
        data_url = self._build_image_data_url(image_path, mime_type)
        if data_url is None:
            return None

        try:
            resp = await self._openai.chat.completions.create(
                model=self._openai_model,
                messages=[
                    {"role": "system", "content": _AI_DNI_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Analizá esta imagen. Primero decidí si es el "
                                    "frente de un DNI argentino y luego extraé los "
                                    "campos pedidos."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            },
                        ],
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=256,
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception:
            logger.exception("DNI image analysis failed")
            return None

        document_type = str(data.get("document_type") or "desconocido").strip().lower()
        is_dni = bool(data.get("is_dni")) or document_type == "dni_frente"
        fecha_nacimiento = self._normalize_birth_date(data.get("birth_date"))
        edad = self._calculate_age(fecha_nacimiento)
        return {
            "document_type": document_type,
            "is_dni": is_dni,
            "nombre": self._normalize_person_name(data.get("full_name")),
            "dni": self._normalize_dni(data.get("dni")),
            "fecha_nacimiento": fecha_nacimiento,
            "edad": edad,
        }

    @staticmethod
    async def _provider_exists(db: Any, user_id: str) -> bool:
        """Return True when the user already has a provider profile."""
        usuario_id = await db.scalar(
            select(UsuarioModel.id).where(UsuarioModel.telefono == user_id)
        )
        if usuario_id is None and user_id.isdigit():
            usuario_id = int(user_id)
        if usuario_id is None:
            return False
        stmt = select(ProviderModel.id).where(ProviderModel.usuario_id == usuario_id)
        return await db.scalar(stmt) is not None

    @staticmethod
    async def _save_state(
        state_repo: EstadoRepository,
        user_id: str,
        *,
        paso: str,
        nombre: str | None = None,
        edad: int | None = None,
        dni: str | None = None,
        fecha_nacimiento: str | None = None,
        rubros: list[str] | None = None,
    ) -> None:
        """Persist the current onboarding step for the next user turn."""
        await state_repo.save(
            user_id,
            {
                "estado": REGISTRATION_STATE_NAME,
                "paso": paso,
                "nombre": nombre,
                "edad": edad,
                "dni": dni,
                "fecha_nacimiento": fecha_nacimiento,
                "rubros": rubros,
            },
        )

    @staticmethod
    def _is_registration_start(
        message: str,
        metadata: dict[str, Any] | None,
    ) -> bool:
        """Return True when the user explicitly asks to register as provider."""
        if metadata and (
            metadata.get("selected_id") in {"register_provider", OFFER_SERVICES_BUTTON_ID}
            or metadata.get("button_id") == OFFER_SERVICES_BUTTON_ID
        ):
            return True

        normalized = ProviderRegistrationService._normalize_message(message)
        return any(pattern in normalized for pattern in REGISTRATION_START_PATTERNS)

    @staticmethod
    def _extract_zone(message: str) -> str | None:
        """Normalize a free-text zone reply such as a barrio or localidad."""
        text = ProviderRegistrationService._strip_prefixes(message, ZONE_PREFIXES)
        text = text.strip(" .,!?:;")
        text = re.sub(
            r"\b(?:zona de|barrio de|localidad de|la zona de|el barrio de)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return None
        return cleaned.title()

    @staticmethod
    def _fallback_trade_labels(raw_message: str) -> list[str]:
        """Map a free-text services reply into up to five canonical rubros."""
        parts = re.split(r",|/|\by\b|\be\b", raw_message, flags=re.IGNORECASE)
        return ProviderRegistrationService._canonicalize_trade_labels(parts)

    @staticmethod
    def _canonicalize_trade_labels(candidates: list[Any]) -> list[str]:
        """Normalize candidate trade labels into unique canonical rubros."""
        labels: list[str] = []
        seen: set[str] = set()
        for part in candidates:
            if not isinstance(part, str):
                continue
            candidate = ProviderRegistrationService._normalize_text(part)
            if len(candidate) < 3:
                continue
            label = resolve_canonical_rubro(candidate)
            if label not in CANONICAL_RUBROS:
                continue
            normalized_label = label.lower()
            if normalized_label in seen:
                continue
            seen.add(normalized_label)
            labels.append(label)
            if len(labels) == 5:
                break
        return labels

    @staticmethod
    def _strip_prefixes(message: str, prefixes: tuple[str, ...]) -> str:
        """Remove a single known conversational prefix from the user reply."""
        normalized = ProviderRegistrationService._normalize_message(message)
        for prefix in prefixes:
            if normalized.startswith(prefix):
                return message[len(prefix) :].strip()
        return message.strip()

    @staticmethod
    def _location_from_metadata(
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Build a reusable location dict from channel metadata."""
        if not metadata:
            return None

        latitude = metadata.get("latitude")
        longitude = metadata.get("longitude")
        ciudad = metadata.get("ciudad")
        barrio = metadata.get("barrio")
        if latitude is None and longitude is None and not ciudad and not barrio:
            return None

        return {
            "lat": float(latitude) if isinstance(latitude, (int, float)) else None,
            "lon": float(longitude) if isinstance(longitude, (int, float)) else None,
            "ciudad": ciudad if isinstance(ciudad, str) else None,
            "barrio": barrio if isinstance(barrio, str) else None,
        }

    @staticmethod
    def _location_label(location: dict[str, Any]) -> str | None:
        """Return the best human-readable label for a provider zone."""
        parts = [location.get("barrio"), location.get("ciudad")]
        labels = [part for part in parts if isinstance(part, str) and part]
        return ", ".join(labels) if labels else None

    @staticmethod
    def _normalize_message(message: str) -> str:
        """Collapse user text into a lowercase string for heuristics."""
        return re.sub(r"\s+", " ", message.strip().lower())

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize accents and whitespace for fuzzy keyword matching."""
        folded = unicodedata.normalize("NFKD", text)
        ascii_text = "".join(char for char in folded if not unicodedata.combining(char))
        lowered = ascii_text.lower().replace("-", " ")
        lowered = re.sub(r"[^a-z0-9ñ ]+", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    @staticmethod
    def _normalize_person_name(value: Any) -> str | None:
        """Sanitize a full name detected by the DNI OCR step."""
        if not isinstance(value, str):
            return None
        words = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ']+", value)
        if not words:
            return None
        return " ".join(word.capitalize() for word in words[:6])

    @staticmethod
    def _normalize_dni(value: Any) -> str | None:
        """Keep only 8-digit DNI values."""
        if value is None:
            return None
        digits = re.sub(r"\D+", "", str(value))
        if len(digits) != 8:
            return None
        return digits

    @staticmethod
    def _normalize_birth_date(value: Any) -> str | None:
        """Normalize a detected birth date into ISO format."""
        if not isinstance(value, str):
            return None

        cleaned = value.strip()
        formats = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d")
        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    @staticmethod
    def _calculate_age(birth_date_iso: str | None) -> int | None:
        """Compute the current age in years from an ISO birth date."""
        if birth_date_iso is None:
            return None
        try:
            birth_date = date.fromisoformat(birth_date_iso)
        except ValueError:
            return None

        today = date.today()
        years = today.year - birth_date.year
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            years -= 1
        if years < 0:
            return None
        return years

    @staticmethod
    def _extract_confirmation(message: str) -> bool | None:
        """Parse a short yes/no confirmation from the user reply."""
        normalized = ProviderRegistrationService._normalize_message(message)
        if normalized in DNI_CONFIRM_YES:
            return True
        if normalized in DNI_CONFIRM_NO:
            return False
        return None

    @staticmethod
    def _build_image_data_url(image_path: str, mime_type: str) -> str | None:
        """Encode a local image file into a data URL for the vision model."""
        try:
            with open(image_path, "rb") as image_file:
                encoded = base64.b64encode(image_file.read()).decode("ascii")
        except OSError:
            logger.exception("Could not read DNI image from %s", image_path)
            return None
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _image_path_from_metadata(metadata: dict[str, Any] | None) -> str | None:
        """Read the local image path saved by the webhook processor."""
        if not metadata:
            return None
        path = metadata.get("image_path")
        return path if isinstance(path, str) and path else None

    @staticmethod
    def _image_mime_type(metadata: dict[str, Any] | None) -> str:
        """Return the image MIME type advertised by the channel metadata."""
        if not metadata:
            return "image/jpeg"
        mime_type = metadata.get("mime_type")
        return mime_type if isinstance(mime_type, str) and mime_type else "image/jpeg"

    @staticmethod
    def _metadata_has_image(metadata: dict[str, Any] | None) -> bool:
        """Return True when the inbound message carries a downloaded image."""
        return ProviderRegistrationService._image_path_from_metadata(metadata) is not None

    @staticmethod
    def _format_birth_date(birth_date_iso: str | None) -> str:
        """Format an ISO date for user-facing confirmation text."""
        if birth_date_iso is None:
            return "no disponible"
        try:
            return date.fromisoformat(birth_date_iso).strftime("%d/%m/%Y")
        except ValueError:
            return birth_date_iso

    @staticmethod
    def _build_dni_request_response(
        retry: bool = False,
        invalid_document: bool = False,
    ) -> AgentResponse:
        """Ask the provider for a clear front photo of the DNI."""
        if invalid_document:
            message = (
                "No pude identificar que la imagen sea el frente de un DNI o no se "
                "ve con suficiente claridad. Mandame una foto más nítida del frente "
                "de tu DNI. Es por temas legales y de seguridad."
            )
        elif retry:
            message = (
                "Necesito una foto clara del frente de tu DNI para seguir con el "
                "registro. Es por temas legales y de seguridad."
            )
        else:
            message = (
                "Para registrarte como prestador necesito una foto clara del frente "
                "de tu DNI. Es por temas legales y de seguridad."
            )
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_dni_processing_error_response() -> AgentResponse:
        """Explain that the DNI photo could not be processed right now."""
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=(
                "No pude analizar la foto del DNI en este momento. Probá de nuevo "
                "con una imagen clara del frente del DNI."
            ),
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_dni_confirmation_response(extracted: dict[str, Any]) -> AgentResponse:
        """Ask the user to confirm the data read from the DNI."""
        message = (
            "Detecté estos datos en tu DNI:\n"
            f"- Nombre y apellido: {extracted['nombre']}\n"
            f"- DNI: {extracted['dni']}\n"
            "- Fecha de nacimiento: "
            f"{ProviderRegistrationService._format_birth_date(extracted['fecha_nacimiento'])}\n"
            "Si está bien respondeme SI. Si no, respondeme NO y mandame otra foto."
        )
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_dni_confirmation_retry_response() -> AgentResponse:
        """Ask for an explicit yes/no confirmation after the DNI OCR step."""
        message = "Respondeme SI para confirmar los datos del DNI o NO para enviar otra foto."
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_underage_response() -> AgentResponse:
        """Reject provider registration when the DNI shows a minor age."""
        message = (
            "No puedo completar el registro de prestador porque el DNI indica "
            "que la persona es menor de edad."
        )
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_trades_request_response(retry: bool = False) -> AgentResponse:
        """Ask for the provider services or trades."""
        message = (
            "Contame qué trabajos sabés hacer o qué servicios ofrecés, "
            "así los clasifico en rubros."
        )
        if retry:
            message = (
                "Necesito que me cuentes qué trabajos hacés u ofrecés para "
                "clasificar tus rubros."
            )
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    @staticmethod
    def _build_zone_request_response(
        rubros: list[str],
        retry: bool = False,
    ) -> AgentResponse:
        """Ask for the provider service zone."""
        rubros_text = ", ".join(rubros)
        message = (
            f"Perfecto, te ubico en {rubros_text}. Ahora decime en qué zona "
            "trabajás o compartime tu ubicación."
        )
        if retry:
            message = (
                "Necesito tu zona de trabajo para terminar el registro. "
                "Podés escribir el barrio/localidad o compartir tu ubicación."
            )
        return AgentResponse(
            intent=Intent.REGISTRAR_PRESTADOR,
            message=message,
            confidence=1.0,
            requires_action=False,
        )

    def _log(self, turn_id: str, event: str, **kwargs: Any) -> None:
        """Forward structured log events to the shared agent logger."""
        if self._alog is None:
            return
        try:
            self._alog.info(turn_id, event, **kwargs)
        except Exception:
            logger.debug("Agent logger failed for event %s", event, exc_info=True)
