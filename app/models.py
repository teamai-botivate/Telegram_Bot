from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base SQLAlchemy model class."""


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(50), unique=True, index=True, nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    active_modules: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    credential: Mapped["TenantDBCredential | None"] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        uselist=False,
    )


class TenantDBCredential(Base):
    __tablename__ = "tenant_db_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="postgresql")

    # Which purchased product this credential row belongs to. Null for legacy single-DB tenants.
    product_slug: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Human-readable label, e.g. "Checklist-Org Production DB".
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stores either Postgres DATABASE_URL or Google Sheet URL (Encrypted)
    connection_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Google Sheets only: encrypted service account JSON string
    google_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stores the auto-discovered structural blueprint
    schema_blueprint: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Auto-generated business rules inferred from schema introspection.
    auto_schema_hints: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    ssl_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="credential")


class TenantQueryExample(Base):
    __tablename__ = "tenant_query_examples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    question_embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    verified_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RegisteredClient(Base):
    __tablename__ = "registered_clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    contact_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    purchased_products: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
    )

    onboarding_tokens: Mapped[list["OnboardingToken"]] = relationship(
        back_populates="registered_client",
        cascade="all, delete-orphan",
    )


class OnboardingToken(Base):
    __tablename__ = "onboarding_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('initial_setup', 'add_database')",
            name="onboarding_tokens_purpose_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    registered_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("registered_clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    product_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    jwt_jti: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    registered_client: Mapped[RegisteredClient] = relationship(back_populates="onboarding_tokens")
