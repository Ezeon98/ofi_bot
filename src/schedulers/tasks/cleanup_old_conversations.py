"""Task that removes conversation records older than 72 hours."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.infrastructure.database.models import ConversationModel
from src.infrastructure.database.session import get_session_factory
from src.schedulers.tasks.base import BaseTask

logger = logging.getLogger(__name__)

_MAX_AGE = timedelta(hours=72)


class CleanupOldConversationsTask(BaseTask):
    """Deletes conversations and all their turns older than 72 hours."""

    async def execute(self, **kwargs: Any) -> None:
        cutoff = datetime.now(timezone.utc) - _MAX_AGE
        factory = get_session_factory()

        async with factory() as session:
            try:
                stmt = _build_delete_statement(cutoff)
                result = await session.execute(stmt)
                await session.commit()
                logger.info(
                    "Cleanup completed | cutoff=%s | deleted=%d",
                    cutoff.isoformat(),
                    result.rowcount,
                )
            except Exception:
                await session.rollback()
                raise


def _build_delete_statement(cutoff: datetime):
    """Return the SQLAlchemy delete for conversations older than *cutoff*."""
    from sqlalchemy import delete

    return delete(ConversationModel).where(ConversationModel.last_message_at < cutoff)