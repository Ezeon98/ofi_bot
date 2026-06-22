"""drop zona from providers, grace_notified from usuarios, add provider_ratings

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove grace_notified column from usuarios
    op.drop_column("usuarios", "grace_notified")

    # Remove zona column and its index from providers
    op.drop_index("ix_providers_zona", table_name="providers")
    op.drop_column("providers", "zona")

    # Create provider_ratings table
    op.create_table(
        "provider_ratings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id", "provider_id", name="uq_provider_rating_user_provider"
        ),
    )
    op.create_index(
        "ix_provider_ratings_provider", "provider_ratings", ["provider_id"]
    )
    op.create_index(
        "ix_provider_ratings_usuario_id", "provider_ratings", ["usuario_id"]
    )


def downgrade() -> None:
    # Drop provider_ratings table
    op.drop_index("ix_provider_ratings_usuario_id", table_name="provider_ratings")
    op.drop_index("ix_provider_ratings_provider", table_name="provider_ratings")
    op.drop_table("provider_ratings")

    # Recreate zona column and index on providers
    op.add_column(
        "providers",
        sa.Column("zona", sa.String(length=100), nullable=False, server_default=""),
    )
    op.create_index("ix_providers_zona", "providers", ["zona"])
    # Remove server_default for downgrade compatibility
    # (SQLAlchemy migration safe approach)

    # Recreate grace_notified column on usuarios
    op.add_column(
        "usuarios",
        sa.Column(
            "grace_notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )