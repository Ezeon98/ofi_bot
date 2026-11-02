"""add provider identity fields

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("dni", sa.String(length=8), nullable=True))
    op.add_column("providers", sa.Column("edad", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_providers_dni_format",
        "providers",
        "(dni IS NULL) OR (dni ~ '^[0-9]{8}$')",
    )
    op.create_check_constraint(
        "ck_providers_edad_range",
        "providers",
        "(edad IS NULL) OR (edad BETWEEN 18 AND 99)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_providers_edad_range", "providers", type_="check")
    op.drop_constraint("ck_providers_dni_format", "providers", type_="check")
    op.drop_column("providers", "edad")
    op.drop_column("providers", "dni")