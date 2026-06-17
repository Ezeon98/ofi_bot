"""Main menu handler — customize with your own options."""

from __future__ import annotations

from src.infrastructure.container import UnitOfWork
from src.infrastructure.external.whatsapp_client import (
    enviar_lista_interactiva,
    enviar_mensaje,
)


async def enviar_menu_principal(uow: UnitOfWork, sender: str) -> None:
    """Send the main menu to the user.

    Customize the sections and rows with your bot's features.
    """
    sections = [
        {
            "title": "Opciones",
            "rows": [
                {"id": "opt_1", "title": "Opción 1", "description": "Descripción de la opción 1"},
                {"id": "opt_2", "title": "Opción 2", "description": "Descripción de la opción 2"},
                {"id": "opt_3", "title": "Opción 3", "description": "Descripción de la opción 3"},
            ],
        },
    ]

    await enviar_lista_interactiva(
        sender,
        "👋 ¡Hola! ¿Qué querés hacer?",
        "Ver opciones",
        sections,
    )
