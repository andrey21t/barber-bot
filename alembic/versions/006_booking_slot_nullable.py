"""Миграция 006 — Booking.slot_id → nullable + drop UNIQUE(slot_id) + drop FK.

revision: 006_booking_slot_nullable
down_revision: 005_workday
created: 2026-08-29

Этап 5.8a «Вариант B»: Booking.slot_id становится Optional — WorkDay path
(booking via 30-min slot generated from WorkDay window) не materializes Slot row,
а использует `Booking.slot_id = NULL` + WorkDay invariant enforcement on
service layer (`_validate_booking_within_workday` + `_check_multi_client_capacity`).

Split с 006 на 008 (Session 5.20 finding E — PLANS.md drift line 89 vs 181/407):
- 006 (этот файл): nullable + drop UNIQUE + drop FK
- 008 (future, после smoke-test 006 на prod ~1 неделя): drop table slots

Что делает upgrade:
1. DROP FK bookings.slot_id → slots.id (Postgres: DROP CONSTRAINT;
   SQLite batch_alter_table drop_constraint). Без drop FK миграция 006 была бы
   заблокирована на 008 (DROP TABLE slots requires FK drop first).
2. DROP UNIQUE INDEX ux_bookings_slot — legacy no-WorkDay path теряет UNIQUE
   guard, остаётся rowcount-check (booking.py:482-487). Достаточно (Postgres
   row-level lock + SQLite DB-lock сериализуют). Critic iter 2 finding 7.
3. ALTER COLUMN slot_id nullable=True — Booking может храниться без slot_id
   (workday-only bookings).

Что НЕ делает upgrade:
- НЕ drop table slots (миграция 008, после smoke-test на prod ~1 неделя)
- НЕ drop column slot_id (оставляем для backwards compat с legacy /book flow
  до 5.8b/c impl, который переключит /slots UI на BookSlot30CallbackData →
  workday path. После 5.8b/c → миграция 009 drop column slot_id, потом 008 drop table)

Cross-DB:
- Postgres: DROP CONSTRAINT IF EXISTS bookings_slot_id_fkey (FK name from
  SQLAlchemy 2.0 default naming convention `<table>_<column>_fkey` — no
  explicit name in 001_initial.py:160 `sa.ForeignKey("slots.id")` → auto-named).
- SQLite: batch_alter_table (creates temp table, copies data, drops old,
  renames). SQLite не имеет named FK constraints в старых версиях, но
  batch_alter_table handles FK drop through copy-rewrite.

Downgrade (one-way door — pet-project single-tenant, data-loss acceptable
для rollback scenario):
1. DELETE FROM bookings WHERE slot_id IS NULL — workday-only bookings не
   имеют matching Slot row → FK re-add не пройдёт без matching Slot.
2. Re-add NOT NULL constraint.
3. Re-add UNIQUE index ux_bookings_slot.
4. Re-add FK bookings.slot_id → slots.id.

One-way door: downgrade удаляет workday-only bookings (data loss). Pet-project,
single-tenant (Екатерина) — acceptable. Production downgrade = rollback
scenario, не routine. Smoke-test на dev-копии перед deploy 006.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006_booking_slot_nullable"
down_revision = "005_workday"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Drop FK on Postgres explicitly — SQLite batch_alter_table handles
    # FK drop via copy-rewrite (no named constraint on older SQLite versions).
    # SQLAlchemy 2.0 default naming convention для unnamed FK:
    # `<table>_<column>_fkey` (001_initial.py:160 `sa.ForeignKey("slots.id")`
    # без explicit name → FK auto-named `bookings_slot_id_fkey` на Postgres).
    # `IF EXISTS` молча skip'ает если имя не совпадает → было F1 (code-reviewer,
    # 2026-08-29): wrong name `fk_bookings_slot_id`
    # silently skip'ал drop → FK оставался → блокировал 008 `DROP TABLE slots`.
    if dialect == "postgresql":
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_slot_id_fkey")

    # 2. Drop UNIQUE index ux_bookings_slot — legacy no-WorkDay path теряет
    # UNIQUE guard, остаётся rowcount-check (booking.py:482-487).
    op.drop_index("ux_bookings_slot", table_name="bookings")

    # 3. ALTER COLUMN slot_id nullable=True via batch_alter_table (SQLite
    # не умеет ALTER COLUMN напрямую — pattern from migration 004:36-47).
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.alter_column(
            "slot_id",
            existing_type=sa.UUID(),
            nullable=True,
        )


def downgrade() -> None:
    # One-way door: workday-only bookings (slot_id IS NULL) не имеют matching
    # Slot row → NOT NULL + FK re-add не пройдёт. Delete them.
    # Pet-project single-tenant — data loss acceptable для rollback scenario.
    op.execute("DELETE FROM bookings WHERE slot_id IS NULL")

    # Re-add NOT NULL constraint.
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.alter_column(
            "slot_id",
            existing_type=sa.UUID(),
            nullable=False,
        )

    # Re-add UNIQUE index ux_bookings_slot.
    op.create_index(
        "ux_bookings_slot",
        "bookings",
        ["slot_id"],
        unique=True,
    )

    # Re-add FK bookings.slot_id → slots.id (batch_alter_table handles
    # cross-DB FK create). Имя симметрично upgrade (SQLAlchemy 2.0 default
    # `<table>_<column>_fkey` = `bookings_slot_id_fkey`) — идемпотентно
    # upgrade → downgrade → upgrade (FK name сохраняется).
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.create_foreign_key(
            "bookings_slot_id_fkey",
            "slots",
            ["slot_id"],
            ["id"],
        )
