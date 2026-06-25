"""cascade delete user_memories and conversations on usuario removal

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # user_memories.user_id → usuarios.telefono (cascade delete)
    op.create_foreign_key(
        "fk_user_memories_telefono",
        "user_memories",
        "usuarios",
        ["user_id"],
        ["telefono"],
        ondelete="CASCADE",
    )

    # conversations.user_id → usuarios.telefono (cascade delete)
    op.create_foreign_key(
        "fk_conversations_telefono",
        "conversations",
        "usuarios",
        ["user_id"],
        ["telefono"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_conversations_telefono", "conversations", type_="foreignkey")
    op.drop_constraint("fk_user_memories_telefono", "user_memories", type_="foreignkey")
