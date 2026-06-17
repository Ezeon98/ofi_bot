"""add AI layer tables: providers, user_memories, conversations, conversation_turns

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── providers ────────────────────────────────────────────────────────
    op.create_table(
        "providers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("rubros", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("zona", sa.String(100), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("disponibilidad", sa.String(200), nullable=True),
        sa.Column("experiencia", sa.String(500), nullable=True),
        sa.Column("facturacion", sa.String(30), nullable=False, server_default="no_factura"),
        sa.Column("plan", sa.String(20), nullable=False, server_default="free"),
        sa.Column("badge_activo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_providers_plan_activo", "providers", ["plan", "activo"])
    op.create_index("ix_providers_zona", "providers", ["zona"])
    op.create_index("ix_providers_usuario_id", "providers", ["usuario_id"])

    # ── user_memories ────────────────────────────────────────────────────
    op.create_table(
        "user_memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(30), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_user_memory_key"),
    )
    op.create_index("ix_user_memories_user_id", "user_memories", ["user_id"])
    op.create_index(
        "ix_user_memories_user_importance", "user_memories", ["user_id", "importance"]
    )

    # ── conversations ────────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "last_message_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    # ── conversation_turns ───────────────────────────────────────────────
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_turns_conversation_id", "conversation_turns", ["conversation_id"])
    op.create_index(
        "ix_turns_conversation_created",
        "conversation_turns",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("conversation_turns")
    op.drop_table("conversations")
    op.drop_table("user_memories")
    op.drop_table("providers")
