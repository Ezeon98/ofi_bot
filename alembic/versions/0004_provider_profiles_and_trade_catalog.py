"""extend providers with normalized location and trade catalog

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


SAMPLE_PHONES = (
    "5491100001001",
    "5491100001002",
    "5491100001003",
    "5491100001004",
    "5491100001005",
    "5491100001006",
)


def upgrade() -> None:
    op.add_column("providers", sa.Column("ciudad", sa.String(length=120), nullable=True))
    op.add_column("providers", sa.Column("barrio", sa.String(length=120), nullable=True))
    op.create_index(
        "ix_providers_ciudad_barrio_activo",
        "providers",
        ["ciudad", "barrio", "activo"],
    )
    op.create_unique_constraint("uq_providers_usuario_id", "providers", ["usuario_id"])
    op.create_check_constraint(
        "ck_providers_active_requires_location",
        "providers",
        "NOT activo OR "
        "(lat IS NOT NULL AND lon IS NOT NULL AND ciudad IS NOT NULL AND barrio IS NOT NULL)",
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("nombre", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nombre", name="uq_trades_nombre"),
        sa.UniqueConstraint("slug", name="uq_trades_slug"),
    )

    op.create_table(
        "provider_trades",
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider_id", "trade_id"),
    )
    op.create_index(
        "ix_provider_trades_trade_provider",
        "provider_trades",
        ["trade_id", "provider_id"],
    )

    op.execute(
        sa.text(
            """
            INSERT INTO usuarios (telefono, nombre, tier, accepted_terms_at, last_interaction)
            VALUES
                ('5491100001001', 'Juan Plomero', 'free', now(), now()),
                ('5491100001002', 'Maria Electricista', 'free', now(), now()),
                ('5491100001003', 'Pedro Gasista', 'free', now(), now()),
                ('5491100001004', 'Laura Pintora', 'free', now(), now()),
                ('5491100001005', 'Diego Jardinero', 'free', now(), now()),
                ('5491100001006', 'Sofia Tecnica', 'free', now(), now())
            ON CONFLICT (telefono) DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO trades (slug, nombre)
            VALUES
                ('plomeria', 'Plomeria'),
                ('electricidad', 'Electricidad'),
                ('gasista', 'Gasista'),
                ('pintura', 'Pintura'),
                ('jardineria', 'Jardineria'),
                ('aire-acondicionado', 'Tecnico en aire acondicionado')
            ON CONFLICT (slug) DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO providers (
                usuario_id,
                nombre,
                rubros,
                zona,
                ciudad,
                barrio,
                lat,
                lon,
                disponibilidad,
                experiencia,
                facturacion,
                plan,
                badge_activo,
                activo
            )
            SELECT
                u.id,
                'Juan Plomero',
                '["Plomeria"]',
                'Palermo, CABA',
                'CABA',
                'Palermo',
                -34.5875,
                -58.4201,
                'Lunes a sabado',
                '8 anos resolviendo urgencias de plomeria en CABA.',
                'monotributo',
                'pro',
                true,
                true
            FROM usuarios u
            WHERE u.telefono = '5491100001001'
            ON CONFLICT ON CONSTRAINT uq_providers_usuario_id DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO providers (
                usuario_id,
                nombre,
                rubros,
                zona,
                ciudad,
                barrio,
                lat,
                lon,
                disponibilidad,
                experiencia,
                facturacion,
                plan,
                badge_activo,
                activo
            )
            SELECT
                u.id,
                'Maria Electricista',
                '["Electricidad"]',
                'Caballito, CABA',
                'CABA',
                'Caballito',
                -34.6183,
                -58.4432,
                'Lunes a viernes',
                'Instalaciones domiciliarias y tableros seccionales.',
                'responsable_inscripto',
                'premium',
                true,
                true
            FROM usuarios u
            WHERE u.telefono = '5491100001002'
            ON CONFLICT ON CONSTRAINT uq_providers_usuario_id DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO providers (
                usuario_id,
                nombre,
                rubros,
                zona,
                ciudad,
                barrio,
                lat,
                lon,
                disponibilidad,
                experiencia,
                facturacion,
                plan,
                badge_activo,
                activo
            )
            SELECT
                u.id,
                'Pedro Gasista',
                '["Gasista", "Plomeria"]',
                'Martinez, San Isidro',
                'San Isidro',
                'Martinez',
                -34.4937,
                -58.5034,
                'Guardias y visitas programadas',
                'Matriculado para instalaciones y deteccion de fugas.',
                'monotributo',
                'pro',
                true,
                true
            FROM usuarios u
            WHERE u.telefono = '5491100001003'
            ON CONFLICT ON CONSTRAINT uq_providers_usuario_id DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO providers (
                usuario_id,
                nombre,
                rubros,
                zona,
                ciudad,
                barrio,
                lat,
                lon,
                disponibilidad,
                experiencia,
                facturacion,
                plan,
                badge_activo,
                activo
            )
            SELECT
                u.id,
                'Laura Pintora',
                '["Pintura"]',
                'Banfield, Lomas de Zamora',
                'Lomas de Zamora',
                'Banfield',
                -34.7446,
                -58.3955,
                'Turnos semanales',
                'Interiores, exteriores y trabajos de terminacion.',
                'monotributo',
                'free',
                false,
                true
            FROM usuarios u
            WHERE u.telefono = '5491100001004'
            ON CONFLICT ON CONSTRAINT uq_providers_usuario_id DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO providers (
                usuario_id,
                nombre,
                rubros,
                zona,
                ciudad,
                barrio,
                lat,
                lon,
                disponibilidad,
                experiencia,
                facturacion,
                plan,
                badge_activo,
                activo
            )
            SELECT
                u.id,
                'Diego Jardinero',
                '["Jardineria"]',
                'La Plata Centro, La Plata',
                'La Plata',
                'Centro',
                -34.9205,
                -57.9536,
                'Lunes a sabado',
                'Mantenimiento de parques, podas y limpieza de jardines.',
                'monotributo',
                'free',
                false,
                true
            FROM usuarios u
            WHERE u.telefono = '5491100001005'
            ON CONFLICT ON CONSTRAINT uq_providers_usuario_id DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO providers (
                usuario_id,
                nombre,
                rubros,
                zona,
                ciudad,
                barrio,
                lat,
                lon,
                disponibilidad,
                experiencia,
                facturacion,
                plan,
                badge_activo,
                activo
            )
            SELECT
                u.id,
                'Sofia Tecnica',
                '["Tecnico en aire acondicionado", "Electricidad"]',
                'Castelar, Moron',
                'Moron',
                'Castelar',
                -34.6512,
                -58.6427,
                'Miercoles a domingo',
                'Instalacion, mantenimiento y reparacion de equipos split.',
                'responsable_inscripto',
                'premium',
                true,
                true
            FROM usuarios u
            WHERE u.telefono = '5491100001006'
            ON CONFLICT ON CONSTRAINT uq_providers_usuario_id DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO provider_trades (provider_id, trade_id)
            SELECT p.id, t.id
            FROM providers p
            JOIN usuarios u ON u.id = p.usuario_id
            JOIN trades t ON t.slug = 'plomeria'
            WHERE u.telefono = '5491100001001'
            ON CONFLICT (provider_id, trade_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO provider_trades (provider_id, trade_id)
            SELECT p.id, t.id
            FROM providers p
            JOIN usuarios u ON u.id = p.usuario_id
            JOIN trades t ON t.slug = 'electricidad'
            WHERE u.telefono = '5491100001002'
            ON CONFLICT (provider_id, trade_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO provider_trades (provider_id, trade_id)
            SELECT p.id, t.id
            FROM providers p
            JOIN usuarios u ON u.id = p.usuario_id
            JOIN trades t ON t.slug IN ('gasista', 'plomeria')
            WHERE u.telefono = '5491100001003'
            ON CONFLICT (provider_id, trade_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO provider_trades (provider_id, trade_id)
            SELECT p.id, t.id
            FROM providers p
            JOIN usuarios u ON u.id = p.usuario_id
            JOIN trades t ON t.slug = 'pintura'
            WHERE u.telefono = '5491100001004'
            ON CONFLICT (provider_id, trade_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO provider_trades (provider_id, trade_id)
            SELECT p.id, t.id
            FROM providers p
            JOIN usuarios u ON u.id = p.usuario_id
            JOIN trades t ON t.slug = 'jardineria'
            WHERE u.telefono = '5491100001005'
            ON CONFLICT (provider_id, trade_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO provider_trades (provider_id, trade_id)
            SELECT p.id, t.id
            FROM providers p
            JOIN usuarios u ON u.id = p.usuario_id
            JOIN trades t ON t.slug IN ('aire-acondicionado', 'electricidad')
            WHERE u.telefono = '5491100001006'
            ON CONFLICT (provider_id, trade_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM usuarios WHERE telefono = ANY(:phones)"
        ).bindparams(sa.bindparam("phones", value=list(SAMPLE_PHONES), expanding=True))
    )
    op.drop_index("ix_provider_trades_trade_provider", table_name="provider_trades")
    op.drop_table("provider_trades")
    op.drop_table("trades")
    op.drop_constraint("ck_providers_active_requires_location", "providers", type_="check")
    op.drop_constraint("uq_providers_usuario_id", "providers", type_="unique")
    op.drop_index("ix_providers_ciudad_barrio_activo", table_name="providers")
    op.drop_column("providers", "barrio")
    op.drop_column("providers", "ciudad")