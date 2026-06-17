"""create usuarios table

Revision ID: 0001
Revises:
Create Date: 2026-06-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usuarios",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telefono", sa.String(30), nullable=False),
        sa.Column(
            "tier",
            sa.Enum("free", "pro", "premium", name="tier_enum"),
            nullable=False,
            server_default="free",
        ),
        sa.Column("nombre", sa.String(100), nullable=True),
        sa.Column(
            "fecha_registro",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("subscription_type", sa.String(30), nullable=True),
        sa.Column("tier_expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "grace_notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("mp_payer_id", sa.String(50), nullable=True),
        sa.Column("mp_subscription_id", sa.String(100), nullable=True),
        sa.Column("mp_subscribed_at", sa.DateTime(), nullable=True),
        sa.Column("bsuid", sa.String(100), nullable=True),
        sa.Column("last_interaction", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telefono"),
        sa.UniqueConstraint("bsuid"),
    )
    op.create_index("ix_usuarios_telefono", "usuarios", ["telefono"])
    op.create_index("ix_usuarios_bsuid", "usuarios", ["bsuid"])
    op.create_index("ix_usuarios_tier_expires", "usuarios", ["tier", "tier_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_usuarios_tier_expires", table_name="usuarios")
    op.drop_index("ix_usuarios_bsuid", table_name="usuarios")
    op.drop_index("ix_usuarios_telefono", table_name="usuarios")
    op.drop_table("usuarios")
    sa.Enum(name="tier_enum").drop(op.get_bind())
