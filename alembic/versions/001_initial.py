"""Initial schema — 7 tables, cross-DB (SQLite dev + Postgres prod).

revision: 001_initial
down_revision: None
created: 2026-08-23

Cross-DB compatibility:
- DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql")
  → SQLite: naive datetime storage; Postgres: TIMESTAMPTZ + asyncpg aware UTC
- BigInteger().with_variant(Integer, "sqlite") for NotificationLog.id
  → SQLite: INTEGER autoincrement; Postgres: BIGINT
- partial indexes with postgresql_where → ignored on SQLite, applied on Postgres
- FK ondelete="CASCADE" → applied on Postgres
  (SQLite enforces via PRAGMA foreign_keys=ON if enabled)
- UUID: Python-side default=uuid.uuid4 (no server-side gen_random_uuid — pet-project simplicity)
- NO EXCLUDE constraint (Postgres-only, separate migration 002 for btree_gist + tstzrange)

Render target: `alembic upgrade head` on empty Postgres at Render preDeployCommand.
Also works on empty SQLite for local dev (Base.metadata.create_all still primary
path in tests/conftest.py — this migration is for prod deploy only, but cross-DB
validity ensures dev/prod parity).
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers used by Alembic.
revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _ts_column() -> Any:
    """TIMESTAMPTZ on Postgres, naive datetime on SQLite (cross-DB)."""
    return sa.DateTime(timezone=True).with_variant(
        postgresql.TIMESTAMP(timezone=True), "postgresql"
    )


def _id_column() -> Any:
    """BigInteger autoincrement on Postgres, Integer on SQLite (cross-DB).

    SQLite autoincrement only works on INTEGER PK, not BIGINT — variant required
    (mirrors bot/models.py:147 NotificationLog.id).
    """
    return sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    # 1. businesses
    op.create_table(
        "businesses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("telegram_owner_id", sa.BigInteger(), nullable=False),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="Europe/Moscow"),
        sa.Column("created_at", _ts_column(), nullable=False, server_default=sa.func.now()),
    )

    # 2. masters
    op.create_table(
        "masters",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.String(50), nullable=False, server_default="barber"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", _ts_column(), nullable=False, server_default=sa.func.now()),
    )
    # Partial index (Postgres-only; ignored on SQLite)
    op.create_index(
        "idx_masters_business",
        "masters",
        ["business_id"],
        postgresql_where=sa.text("is_active = TRUE"),
    )

    # 3. services
    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", _ts_column(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("duration_minutes > 0", name="ck_service_duration_positive"),
    )
    op.create_index(
        "idx_services_business",
        "services",
        ["business_id"],
        postgresql_where=sa.text("is_active = TRUE"),
    )

    # 4. clients
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # telegram_id is UNIQUE — implicit unique index (mirrors models.py:78)
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("created_at", _ts_column(), nullable=False, server_default=sa.func.now()),
    )

    # 5. slots
    op.create_table(
        "slots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "master_id",
            sa.Uuid(),
            sa.ForeignKey("masters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slot_date", sa.Date(), nullable=False),
        sa.Column("slot_hour", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", _ts_column(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("slot_hour BETWEEN 0 AND 23", name="ck_slot_hour_range"),
    )
    # Composite UNIQUE (master_id, slot_date, slot_hour)
    op.create_index(
        "ux_slots_master_date_hour",
        "slots",
        ["master_id", "slot_date", "slot_hour"],
        unique=True,
    )
    # Partial index — only open slots
    op.create_index(
        "idx_slots_master_date",
        "slots",
        ["master_id", "slot_date"],
        postgresql_where=sa.text("status = 'open'"),
    )

    # 6. bookings
    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slot_id", sa.Uuid(), sa.ForeignKey("slots.id"), nullable=False),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column(
            "master_id",
            sa.Uuid(),
            sa.ForeignKey("masters.id"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("clients.id"),
            nullable=False,
        ),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("services.id"), nullable=True),
        sa.Column("service_title_snapshot", sa.String(255), nullable=False),
        sa.Column("service_price_snapshot", sa.Numeric(10, 2), nullable=True),
        sa.Column("client_name_snapshot", sa.String(255), nullable=False),
        sa.Column("start_at", _ts_column(), nullable=False),
        sa.Column("end_at", _ts_column(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="confirmed"),
        sa.Column("created_at", _ts_column(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("end_at > start_at", name="ck_booking_duration_positive"),
    )
    # UNIQUE(slot_id) — SQLite fallback for no-double-booking (no EXCLUDE constraint)
    op.create_index("ux_bookings_slot", "bookings", ["slot_id"], unique=True)
    # Partial index for /mybookings (only active bookings)
    op.create_index(
        "idx_bookings_client",
        "bookings",
        ["client_id"],
        postgresql_where=sa.text("status IN ('confirmed', 'transferred')"),
    )
    # Regular composite index for /week queries (master + date range via start_at)
    op.create_index("idx_bookings_master_start", "bookings", ["master_id", "start_at"])

    # 7. notifications_log
    op.create_table(
        "notifications_log",
        sa.Column("id", _id_column(), primary_key=True, autoincrement=True),
        sa.Column(
            "booking_id",
            sa.Uuid(),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("sent_at", _ts_column(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('remind_24h','remind_1h','master_new','master_cancel','master_transfer')",
            name="ck_notifications_kind",
        ),
    )
    # UNIQUE(booking_id, kind) — idempotency guard
    op.create_index(
        "ux_notifications_booking_kind",
        "notifications_log",
        ["booking_id", "kind"],
        unique=True,
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_index("ux_notifications_booking_kind", table_name="notifications_log")
    op.drop_table("notifications_log")

    op.drop_index("idx_bookings_master_start", table_name="bookings")
    op.drop_index("idx_bookings_client", table_name="bookings")
    op.drop_index("ux_bookings_slot", table_name="bookings")
    op.drop_table("bookings")

    op.drop_index("idx_slots_master_date", table_name="slots")
    op.drop_index("ux_slots_master_date_hour", table_name="slots")
    op.drop_table("slots")

    op.drop_table("clients")

    op.drop_index("idx_services_business", table_name="services")
    op.drop_table("services")

    op.drop_index("idx_masters_business", table_name="masters")
    op.drop_table("masters")

    op.drop_table("businesses")
