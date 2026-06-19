"""create missing state and message count tables

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usuario_estado",
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(length=50), nullable=False),
        sa.Column("datos", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("actualizado", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("usuario_id"),
    )

    op.create_table(
        "message_counts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("fecha", sa.String(length=20), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usuario_id", "fecha"),
    )
    op.create_index("ix_message_counts_usuario_id", "message_counts", ["usuario_id"])


def downgrade() -> None:
    op.drop_index("ix_message_counts_usuario_id", table_name="message_counts")
    op.drop_table("message_counts")
    op.drop_table("usuario_estado")