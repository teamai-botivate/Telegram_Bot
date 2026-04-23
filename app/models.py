from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
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
    db_type: Mapped[str] = mapped_column(String(32), nullable=False)
    
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
