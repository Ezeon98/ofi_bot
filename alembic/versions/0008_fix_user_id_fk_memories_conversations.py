"""Fix user_id FK in user_memories and conversations to reference usuarios.id (int) instead of usuarios.telefono (varchar).

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── user_memories ────────────────────────────────────────────────────────

    # 1. Add temp integer column
    op.add_column("user_memories", sa.Column("_uid", sa.Integer(), nullable=True))

    # 2. Fill from usuarios lookup
    op.execute(
        """
        UPDATE user_memories um
        SET _uid = u.id
        FROM usuarios u
        WHERE u.telefono = um.user_id
        """
    )

    # 3. Delete orphans (no matching phone → user)
    op.execute("DELETE FROM user_memories WHERE _uid IS NULL")

    # 4. Drop FK, index, and old column
    op.drop_index("ix_user_memories_user_importance", table_name="user_memories")
    op.drop_index("ix_user_memories_user_id", table_name="user_memories")
    op.drop_constraint("uq_user_memory_key", "user_memories", type_="unique")

    # Drop the FK constraint (Postgres auto-names it)
    op.execute(
        """
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN SELECT conname FROM pg_constraint
                     WHERE conrelid = 'user_memories'::regclass AND contype = 'f'
                     AND conname LIKE '%user_id%'
            LOOP
                EXECUTE 'ALTER TABLE user_memories DROP CONSTRAINT ' || quote_ident(r.conname);
            END LOOP;
        END$$;
        """
    )

    op.drop_column("user_memories", "user_id")

    # 5. Rename temp column and add constraints
    op.alter_column("user_memories", "_uid", new_column_name="user_id", nullable=False)

    op.create_index("ix_user_memories_user_id", "user_memories", ["user_id"])
    op.create_index("ix_user_memories_user_importance", "user_memories", ["user_id", "importance"])
    op.create_unique_constraint("uq_user_memory_key", "user_memories", ["user_id", "key"])
    op.create_foreign_key(
        "fk_user_memories_user_id",
        "user_memories", "usuarios",
        ["user_id"], ["id"],
        ondelete="CASCADE",
    )

    # ── conversations ────────────────────────────────────────────────────────

    # 1. Add temp integer column
    op.add_column("conversations", sa.Column("_uid", sa.Integer(), nullable=True))

    # 2. Fill from usuarios lookup
    op.execute(
        """
        UPDATE conversations c
        SET _uid = u.id
        FROM usuarios u
        WHERE u.telefono = c.user_id
        """
    )

    # 3. Delete orphans
    op.execute("DELETE FROM conversations WHERE _uid IS NULL")

    # 4. Drop FK, index, and old column
    op.drop_index("ix_conversations_user_id", table_name="conversations")

    op.execute(
        """
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN SELECT conname FROM pg_constraint
                     WHERE conrelid = 'conversations'::regclass AND contype = 'f'
                     AND conname LIKE '%user_id%'
            LOOP
                EXECUTE 'ALTER TABLE conversations DROP CONSTRAINT ' || quote_ident(r.conname);
            END LOOP;
        END$$;
        """
    )

    op.drop_column("conversations", "user_id")

    # 5. Rename and add constraints
    op.alter_column("conversations", "_uid", new_column_name="user_id", nullable=False)

    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_foreign_key(
        "fk_conversations_user_id",
        "conversations", "usuarios",
        ["user_id"], ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Reverse: user_memories
    op.add_column("user_memories", sa.Column("_phone", sa.String(30), nullable=True))
    op.execute(
        """
        UPDATE user_memories um
        SET _phone = u.telefono
        FROM usuarios u
        WHERE u.id = um.user_id
        """
    )
    op.drop_constraint("fk_user_memories_user_id", "user_memories", type_="foreignkey")
    op.drop_index("ix_user_memories_user_importance", table_name="user_memories")
    op.drop_index("ix_user_memories_user_id", table_name="user_memories")
    op.drop_constraint("uq_user_memory_key", "user_memories", type_="unique")
    op.drop_column("user_memories", "user_id")
    op.alter_column("user_memories", "_phone", new_column_name="user_id", nullable=False)
    op.create_index("ix_user_memories_user_id", "user_memories", ["user_id"])
    op.create_index("ix_user_memories_user_importance", "user_memories", ["user_id", "importance"])
    op.create_unique_constraint("uq_user_memory_key", "user_memories", ["user_id", "key"])
    op.create_foreign_key(
        None, "user_memories", "usuarios", ["user_id"], ["telefono"], ondelete="CASCADE"
    )

    # Reverse: conversations
    op.add_column("conversations", sa.Column("_phone", sa.String(30), nullable=True))
    op.execute(
        """
        UPDATE conversations c
        SET _phone = u.telefono
        FROM usuarios u
        WHERE u.id = c.user_id
        """
    )
    op.drop_constraint("fk_conversations_user_id", "conversations", type_="foreignkey")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_column("conversations", "user_id")
    op.alter_column("conversations", "_phone", new_column_name="user_id", nullable=False)
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_foreign_key(
        None, "conversations", "usuarios", ["user_id"], ["telefono"], ondelete="CASCADE"
    )
