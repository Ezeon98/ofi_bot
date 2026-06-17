"""Database access for user memories and conversations.

All methods are async and operate on an injected AsyncSession.
No business logic here — pure persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.models import (
    ConversationModel,
    ConversationTurnModel,
    UserMemoryModel,
)
from src.memory.schemas import (
    ConversationRead,
    ConversationTurnRead,
    MemoryRead,
    MemoryUpsert,
)


class MemoryRepository:
    """CRUD for user_memories table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user(
        self,
        user_id: str,
        min_importance: float = 0.0,
        limit: int = 50,
    ) -> list[MemoryRead]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(UserMemoryModel)
            .where(
                UserMemoryModel.user_id == user_id,
                UserMemoryModel.importance >= min_importance,
                (UserMemoryModel.expires_at == None) | (UserMemoryModel.expires_at > now),  # noqa: E711
            )
            .order_by(UserMemoryModel.importance.desc())
            .limit(limit)
        )
        rows = await self._session.scalars(stmt)
        return [MemoryRead.model_validate(r) for r in rows]

    async def upsert(self, user_id: str, data: MemoryUpsert) -> MemoryRead:
        """Insert or update a memory by (user_id, key)."""
        stmt = select(UserMemoryModel).where(
            UserMemoryModel.user_id == user_id,
            UserMemoryModel.key == data.key,
        )
        existing = await self._session.scalar(stmt)
        if existing:
            existing.value = data.value
            existing.importance = data.importance
            existing.expires_at = data.expires_at
            existing.updated_at = datetime.now(timezone.utc)
            await self._session.flush()
            return MemoryRead.model_validate(existing)

        record = UserMemoryModel(
            user_id=user_id,
            key=data.key,
            value=data.value,
            importance=data.importance,
            expires_at=data.expires_at,
        )
        self._session.add(record)
        await self._session.flush()
        return MemoryRead.model_validate(record)

    async def delete_by_key(self, user_id: str, key: str) -> None:
        await self._session.execute(
            delete(UserMemoryModel).where(
                UserMemoryModel.user_id == user_id,
                UserMemoryModel.key == key,
            )
        )

    async def count_by_user(self, user_id: str) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).where(UserMemoryModel.user_id == user_id)
        return await self._session.scalar(stmt) or 0

    async def drop_least_important(self, user_id: str, keep: int) -> None:
        """Delete memories beyond `keep`, discarding lowest-importance ones first."""
        stmt = (
            select(UserMemoryModel.id)
            .where(UserMemoryModel.user_id == user_id)
            .order_by(UserMemoryModel.importance.desc())
            .offset(keep)
        )
        ids_to_drop = list(await self._session.scalars(stmt))
        if ids_to_drop:
            await self._session.execute(
                delete(UserMemoryModel).where(UserMemoryModel.id.in_(ids_to_drop))
            )


class ConversationRepository:
    """CRUD for conversations + conversation_turns."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_active(self, user_id: str) -> ConversationRead:
        """Return the active (most recent) conversation, creating one if needed."""
        stmt = (
            select(ConversationModel)
            .where(ConversationModel.user_id == user_id)
            .order_by(ConversationModel.last_message_at.desc())
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        if row is None:
            row = ConversationModel(user_id=user_id)
            self._session.add(row)
            await self._session.flush()
        return ConversationRead.model_validate(row)

    async def get_recent_turns(
        self, conversation_id: int, limit: int = 20
    ) -> list[ConversationTurnRead]:
        stmt = (
            select(ConversationTurnModel)
            .where(ConversationTurnModel.conversation_id == conversation_id)
            .order_by(ConversationTurnModel.created_at.desc())
            .limit(limit)
        )
        rows = list(await self._session.scalars(stmt))
        rows.reverse()  # chronological order
        return [ConversationTurnRead.model_validate(r) for r in rows]

    async def add_turn(
        self,
        conversation_id: int,
        role: str,
        content: str,
        intent: str | None = None,
    ) -> ConversationTurnRead:
        turn = ConversationTurnModel(
            conversation_id=conversation_id,
            role=role,
            content=content,
            intent=intent,
        )
        self._session.add(turn)
        # touch last_message_at
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(last_message_at=datetime.now(timezone.utc))
        )
        await self._session.flush()
        return ConversationTurnRead.model_validate(turn)

    async def count_turns(self, conversation_id: int) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).where(
            ConversationTurnModel.conversation_id == conversation_id
        )
        return await self._session.scalar(stmt) or 0

    async def update_summary(self, conversation_id: int, summary: str) -> None:
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(summary=summary)
        )

    async def delete_turns_before_offset(
        self, conversation_id: int, keep_last: int
    ) -> None:
        """Delete all turns except the most recent `keep_last`."""
        stmt = (
            select(ConversationTurnModel.id)
            .where(ConversationTurnModel.conversation_id == conversation_id)
            .order_by(ConversationTurnModel.created_at.desc())
            .offset(keep_last)
        )
        ids_to_drop = list(await self._session.scalars(stmt))
        if ids_to_drop:
            await self._session.execute(
                delete(ConversationTurnModel).where(
                    ConversationTurnModel.id.in_(ids_to_drop)
                )
            )
