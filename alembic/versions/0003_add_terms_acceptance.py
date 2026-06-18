"""add accepted_terms_at to usuarios

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usuarios", sa.Column("accepted_terms_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("usuarios", "accepted_terms_at")