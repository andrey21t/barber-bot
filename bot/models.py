import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from bot.db import Base

# SQLAlchemy 2.0 + cross-DB types (Uuid + DateTime(timezone=True) работают на SQLite и Postgres).
# Postgres: with_variant(TIMESTAMP(timezone=True), "postgresql") → TIMESTAMPTZ column,
# asyncpg returns aware UTC by default (regardless of session TZ — asyncpg normalizes
# to UTC on the client side). `.replace(tzinfo=UTC)` in service code is no-op on Postgres
# (already aware UTC), makes naive aware on SQLite. If connection timezone is changed
# to non-UTC and a non-asyncpg driver returns non-UTC aware, use `.astimezone(UTC)` instead
# (but that breaks SQLite naive path — would need DB-dialect branching).
# SQLite: DateTime(timezone=True) base → naive datetime
# (verified empirically: scratch stub 2026-08-23 — same as without variant).


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    telegram_owner_id: Mapped[int] = mapped_column(BigInteger)
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Moscow")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
    )


class Master(Base):
    __tablename__ = "masters"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("businesses.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255))
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role: Mapped[str] = mapped_column(String(50), default="barber")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_masters_business", "business_id", postgresql_where=text("is_active = TRUE")),
    )


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("businesses.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255))
    duration_minutes: Mapped[int] = mapped_column()
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="ck_service_duration_positive"),
        Index("idx_services_business", "business_id", postgresql_where=text("is_active = TRUE")),
    )


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
    )


class Slot(Base):
    __tablename__ = "slots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    master_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("masters.id", ondelete="CASCADE"), nullable=False
    )
    slot_date: Mapped[date] = mapped_column()
    slot_hour: Mapped[int] = mapped_column()  # LOCAL hour in business.timezone
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | booked | closed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("slot_hour BETWEEN 0 AND 23", name="ck_slot_hour_range"),
        # UNIQUE (master_id, slot_date, slot_hour) — composite unique via Index
        Index("ux_slots_master_date_hour", "master_id", "slot_date", "slot_hour", unique=True),
        Index(
            "idx_slots_master_date",
            "master_id",
            "slot_date",
            postgresql_where=text("status = 'open'"),
        ),
    )


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("slots.id"), nullable=False)
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("businesses.id"), nullable=False
    )
    master_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("masters.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("clients.id"), nullable=False)
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("services.id"), nullable=True
    )
    service_title_snapshot: Mapped[str] = mapped_column(String(255))  # html.escape()'d
    service_price_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    client_name_snapshot: Mapped[str] = mapped_column(String(255))  # html.escape()'d
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql")
    )  # UTC
    end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql")
    )  # UTC
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("end_at > start_at", name="ck_booking_duration_positive"),
        Index("ux_bookings_slot", "slot_id", unique=True),  # UNIQUE(slot_id) — SQLite fallback
        Index(
            "idx_bookings_client",
            "client_id",
            postgresql_where=text("status IN ('confirmed', 'transferred')"),
        ),
        Index("idx_bookings_master_start", "master_id", "start_at"),
    )


class NotificationLog(Base):
    __tablename__ = "notifications_log"

    # SQLite не autoincrement на BIGINT — variant на sqlite = INTEGER (autoincrement работает)
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30))
    # remind_24h | remind_1h | master_new | master_cancel | master_transfer
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('remind_24h','remind_1h','master_new','master_cancel','master_transfer')",
            name="ck_notifications_kind",
        ),
        Index("ux_notifications_booking_kind", "booking_id", "kind", unique=True),
    )


class FsmState(Base):
    """FSM state persistence (spec.md:538-547 — PostgresStorage prod).

    Cross-DB design (deep-analysis Session 4):
    - Postgres: JSONB column → atomic `data || $new::jsonb` merge in update_data
      (no race condition, no SimpleEventIsolation needed).
    - SQLite (dev/test): JSON column → update_data uses read-modify-write
      (base.py default impl). Single-process test → race not triggered.

    PK = composite (bot_id, chat_id, user_id, destiny) — covers all
    StorageKey fields except thread_id/business_connection_id (NULL for simple
    chats). See aiogram StorageKey (fsm/storage/base.py:14-21).

    Values stored as JSON-serializable (verified existing handlers use
    str(uuid) and ISO datetime strings — no custom encoder needed):
    - client.py:239 `state.update_data(slot_id=str(callback_data.slot_id))`
    - client.py:698 `state.update_data(transfer_booking_id=str(...))`
    - session_timeout.py:61 `update_data(last_message_at=...isoformat())`
    """

    __tablename__ = "fsm_states"

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    destiny: Mapped[str] = mapped_column(String(50), primary_key=True, default="default")
    state: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default="{}",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql"),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (Index("idx_fsm_states_chat_user", "chat_id", "user_id"),)
