"""WhatsApp Business Cloud API client."""

import json
import logging
import os
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from src.infrastructure.config import get_settings

logger = logging.getLogger(__name__)


def normalize_phone(telefono: str) -> str:
    """Transform '54911XXXXXXXX' to '541115XXXXXXXX' (Argentina format)."""
    if telefono.startswith("54911") and len(telefono) == 13:
        return "541115" + telefono[5:]
    return telefono


def _resp_body(resp: httpx.Response) -> str:
    try:
        return json.dumps(resp.json(), ensure_ascii=False)
    except Exception:
        return resp.text


def _settings():
    return get_settings()


def build_whatsapp_contact_url(telefono: str, message: str) -> str:
    """Build a cross-platform WhatsApp chat URL for a prefilled message."""
    phone_digits = "".join(char for char in telefono if char.isdigit())
    query = urlencode(
        {
            "phone": phone_digits,
            "text": message,
            "type": "phone_number",
            "app_absent": "0",
        },
        quote_via=quote,
    )
    return f"https://api.whatsapp.com/send?{query}"


async def enviar_mensaje(telefono: str, texto: str) -> None:
    """Send a text message."""
    if not texto or not texto.strip():
        logger.warning("Empty message to %s — ignored.", telefono)
        return
    s = _settings()
    telefono = normalize_phone(telefono)
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": texto},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        if resp.status_code != 200:
            logger.error("Error sending message to %s: %s %s", telefono, resp.status_code, _resp_body(resp))


async def enviar_typing(telefono: str, message_id: str = "") -> None:
    """Send a typing indicator ("escribiendo...") to the user."""
    s = _settings()
    if not message_id:
        logger.warning("typing indicator skipped for %s: missing message_id", telefono)
        return
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        if resp.status_code != 200:
            logger.warning(
                "typing indicator failed for %s: %s %s",
                telefono,
                resp.status_code,
                _resp_body(resp),
            )


async def enviar_imagen(telefono: str, archivo_local: str, caption: str = "") -> None:
    """Upload and send an image."""
    s = _settings()
    headers_upload = {"Authorization": f"Bearer {s.whatsapp_token.get_secret_value()}"}
    async with httpx.AsyncClient() as client:
        with open(archivo_local, "rb") as f:
            resp_upload = await client.post(
                s.meta_media_url,
                headers=headers_upload,
                files={"file": ("image.png", f, "image/png")},
                data={"messaging_product": "whatsapp", "type": "image/png"},
            )
        if resp_upload.status_code != 200:
            logger.error("Error uploading image: %s %s", resp_upload.status_code, resp_upload.text)
            return
        media_id = resp_upload.json().get("id")
        telefono = normalize_phone(telefono)
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "image",
            "image": {"id": media_id, "caption": caption},
        }
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        if resp.status_code != 200:
            logger.error("Error sending image: %s", resp.status_code)


_MIME_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}


async def enviar_documento(
    telefono: str, archivo_local: str, nombre_archivo: str, caption: str = ""
) -> None:
    """Upload and send a document."""
    s = _settings()
    headers_upload = {"Authorization": f"Bearer {s.whatsapp_token.get_secret_value()}"}
    ext = os.path.splitext(archivo_local)[1].lower()
    mime = _MIME_TYPES.get(ext, "application/octet-stream")
    async with httpx.AsyncClient() as client:
        with open(archivo_local, "rb") as f:
            resp_upload = await client.post(
                s.meta_media_url,
                headers=headers_upload,
                files={"file": (nombre_archivo, f, mime)},
                data={"messaging_product": "whatsapp", "type": mime},
            )
        if resp_upload.status_code != 200:
            logger.error("Error uploading document: %s", resp_upload.status_code)
            return
        media_id = resp_upload.json().get("id")
        doc_payload: dict[str, Any] = {"id": media_id, "filename": nombre_archivo}
        if caption:
            doc_payload["caption"] = caption
        telefono = normalize_phone(telefono)
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "document",
            "document": doc_payload,
        }
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        if resp.status_code != 200:
            logger.error("Error sending document: %s", resp.status_code)


def _truncate_list_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce WhatsApp interactive list limits."""
    for section in sections:
        title = section.get("title", "")
        if len(title) > 24:
            section["title"] = title[:24]
        for row in section.get("rows", []):
            rt = row.get("title", "")
            if len(rt) > 24:
                row["title"] = rt[:23] + "…"
            rd = row.get("description", "")
            if len(rd) > 72:
                row["description"] = rd[:71] + "…"
    return sections


async def enviar_lista_interactiva(
    telefono: str, body_text: str, button_text: str, sections: list[dict[str, Any]]
) -> None:
    """Send an interactive list message."""
    s = _settings()
    telefono = normalize_phone(telefono)
    button_text = button_text[:20] if len(button_text) > 20 else button_text
    sections = _truncate_list_sections(sections)
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {"button": button_text, "sections": sections},
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        if resp.status_code != 200:
            logger.error("Error sending list to %s: %s %s", telefono, resp.status_code, _resp_body(resp))


async def enviar_botones_respuesta(
    telefono: str,
    body_text: str,
    buttons: list[dict[str, str]],
) -> None:
    """Send an interactive message with quick-reply buttons (max 3)."""
    s = _settings()
    telefono = normalize_phone(telefono)
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons[:3]
                ]
            },
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        if resp.status_code != 200:
            logger.error("Error sending buttons to %s: %s %s", telefono, resp.status_code, _resp_body(resp))


async def enviar_boton_cta(telefono: str, body_text: str, display_text: str, url: str) -> None:
    """Send a CTA URL button."""
    s = _settings()
    telefono = normalize_phone(telefono)
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {"name": "cta_url", "parameters": {"display_text": display_text, "url": url}},
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        if resp.status_code != 200:
            logger.error("Error sending CTA: %s", resp.status_code)


async def enviar_reaccion(telefono: str, message_id: str, emoji: str) -> None:
    """Send a reaction to a message."""
    s = _settings()
    telefono = normalize_phone(telefono)
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": telefono,
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(s.meta_api_url, headers=s.meta_headers, json=payload)
        logger.debug("Reaction %s → %s", emoji, resp.status_code)


async def quitar_reaccion(telefono: str, message_id: str) -> None:
    """Remove a reaction from a message."""
    await enviar_reaccion(normalize_phone(telefono), message_id, "")


async def descargar_media(media_id: str, destino: str) -> None:
    """Download a media file from WhatsApp."""
    s = _settings()
    headers = {"Authorization": f"Bearer {s.whatsapp_token.get_secret_value()}"}
    async with httpx.AsyncClient() as client:
        info = await client.get(f"https://graph.facebook.com/v22.0/{media_id}", headers=headers)
        info.raise_for_status()
        url = info.json().get("url")
        media = await client.get(url, headers=headers)
        media.raise_for_status()
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "wb") as f:
        f.write(media.content)
