"""SQLAlchemy ORM models — minimal base for WhatsApp bot."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UsuarioModel(Base):
    """User model."""

    __tablename__ = "usuarios"
    __table_args__ = (
        Index("ix_usuarios_tier_expires", "tier", "tier_expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    tier: Mapped[str] = mapped_column(
        Enum("free", "pro", "premium", name="tier_enum"),
        nullable=False,
        default="free",
        server_default="free",
    )
    nombre: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fecha_registro: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    # Subscription management
    subscription_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tier_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    grace_notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # MercadoPago subscription fields
    mp_payer_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mp_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mp_subscribed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Business-Scoped User ID (Meta BSUID migration)
    bsuid: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True, index=True)
    # Last interaction for 24h messaging window
    last_interaction: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_terms_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EstadoUsuarioModel(Base):
    """Per-user conversation state (state machine)."""

    __tablename__ = "usuario_estado"

    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuarios.id", ondelete="CASCADE"), primary_key=True
    )
    estado: Mapped[str] = mapped_column(String(50), nullable=False)
    datos: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    actualizado: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MessageCountModel(Base):
    """Daily message count per user."""

    __tablename__ = "message_counts"
    __table_args__ = (UniqueConstraint("usuario_id", "fecha"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fecha: Mapped[str] = mapped_column(String(20), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MercadoPagoSubscriptionModel(Base):
    """Active MP subscriptions (preapprovals) linked to users."""

    __tablename__ = "mp_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mp_preapproval_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    mp_payer_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    plan_id: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payer_email: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="authorized", server_default="authorized"
    )
    next_payment_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_payment_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    external_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)


# ── AI layer models ───────────────────────────────────────────────────────────


class ProviderModel(Base):
    """Service provider (prestador) profile.

    Draft rows can exist before the provider completes onboarding.
    Only active providers are expected to have full searchable location data.
    """

    __tablename__ = "providers"
    __table_args__ = (
        Index("ix_providers_plan_activo", "plan", "activo"),
        Index("ix_providers_zona", "zona"),
        Index("ix_providers_ciudad_barrio_activo", "ciudad", "barrio", "activo"),
        UniqueConstraint("usuario_id", name="uq_providers_usuario_id"),
        CheckConstraint(
            "NOT activo OR "
            "(lat IS NOT NULL AND lon IS NOT NULL AND ciudad IS NOT NULL AND barrio IS NOT NULL)",
            name="ck_providers_active_requires_location",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    rubros: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON array
    zona: Mapped[str] = mapped_column(String(100), nullable=False)
    ciudad: Mapped[str | None] = mapped_column(String(120), nullable=True)
    barrio: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    disponibilidad: Mapped[str | None] = mapped_column(String(200), nullable=True)
    experiencia: Mapped[str | None] = mapped_column(String(500), nullable=True)
    facturacion: Mapped[str] = mapped_column(
        String(30), nullable=False, default="no_factura", server_default="no_factura"
    )
    plan: Mapped[str] = mapped_column(
        String(20), nullable=False, default="free", server_default="free"
    )
    badge_activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TradeModel(Base):
    """Normalized catalog of provider trades/officios."""

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_trades_slug"),
        UniqueConstraint("nombre", name="uq_trades_nombre"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class ProviderTradeModel(Base):
    """Many-to-many link between providers and trades."""

    __tablename__ = "provider_trades"
    __table_args__ = (Index("ix_provider_trades_trade_provider", "trade_id", "provider_id"),)

    provider_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("providers.id", ondelete="CASCADE"), primary_key=True
    )
    trade_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class UserMemoryModel(Base):
    """Persistent key-value memory for each user, managed by the AI agent."""

    __tablename__ = "user_memories"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_user_memory_key"),
        Index("ix_user_memories_user_importance", "user_id", "importance"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ConversationModel(Base):
    """One conversation session per user (rolling window)."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConversationTurnModel(Base):
    """Individual message turn within a conversation."""

    __tablename__ = "conversation_turns"
    __table_args__ = (
        Index("ix_turns_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
