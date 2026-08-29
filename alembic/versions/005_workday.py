"""Миграция 005 — WorkDay таблица + перенос данных из Slot + DROP EXCLUDE.

revision: 005_workday
down_revision: 004_service_price_nullable
created: 2026-08-28

Этап 5.2 «Вариант B»: переход от Slot (slot_hour:int) к WorkDay (start_time/end_time:time).

Что делает upgrade:
1. CREATE TABLE work_days (master_id, work_date, start_time, end_time,
   max_concurrent_clients=1, is_active=True)
   + UNIQUE(master_id, work_date) — идемпотент /openday на ту же дату (UPDATE, не INSERT)
2. Перенос данных из slots: для каждого (master_id, slot_date) — окно
   [min(slot_hour), max(slot_hour)+60min]. Буфер 60мин покрывает 60-мин услуги
   (если есть услуги >60мин — data-fix в 5.3, расширение WorkDay.end_time).
   Edge: max_h=23 → +30мин (не +60мин, чтобы не перейти в полночь →
   CheckConstraint violation на end_time < start_time).
   Multi-client capacity = 1 (для Екатерины UPDATE до 2 в 5.5).
3. DROP EXCLUDE constraint no_overlap (миграция 002) на Postgres — ломает multi-client
   (запрещает любые overlap'ы, multi-client = разрешённые overlap'ы).
   Замена: app-level check + retry + pg_advisory_xact_lock (BB-008, в 5.5).

Что НЕ делает upgrade:
- НЕ drop table slots (миграция 008, после smoke-test 006 на prod ~1 неделя)
- НЕ drop Booking.slot_id FK (миграция 006)
- НЕ drop UNIQUE(slot_id) на bookings (миграция 006)

Cross-DB:
- Postgres: DROP CONSTRAINT no_overlap (EXCLUDE constraint from 002)
- SQLite: no-op (002 был no-op на SQLite — UNIQUE(slot_id) на bookings handles
  same-slot double-booking; остаётся до миграции 006)

One-way door: данные Екатерины на VPS (live bookings).
Тестировать на dev-копии prod базы перед deploy.
См. PLANS.md Decision Log Blocker B + Gap 6.

Downgrade: DROP TABLE work_days (без восстановления EXCLUDE — обратный перенос
WorkDay→Slot не тривиален; миграция 005 объявлена one-way в проде после smoke-test).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import sqlalchemy as sa
from alembic import op

revision = "005_workday"
down_revision = "004_service_price_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. CREATE TABLE work_days (cross-DB через SQLAlchemy types — без server_default,
    #    соответствует стилю Slot в 001_initial: default на уровне Python ORM, не SQL)
    op.create_table(
        "work_days",
        sa.Column("id", sa.Uuid, primary_key=True, nullable=False),
        sa.Column(
            "master_id",
            sa.Uuid,
            sa.ForeignKey("masters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("work_date", sa.Date, nullable=False),
        sa.Column("start_time", sa.Time, nullable=False),
        sa.Column("end_time", sa.Time, nullable=False),
        # server_default для raw SQL INSERT (psql, restore-скрипты, будущие миграции).
        # Соответствует паттерну 001_initial (Master.is_active, Service.is_active
        # используют server_default=sa.true()). Python default в ORM-модели
        # (bot/models.py WorkDay) применяется при ORM INSERT.
        sa.Column(
            "max_concurrent_clients", sa.Integer, nullable=False, server_default=sa.text("1")
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True).with_variant(
                sa.dialects.postgresql.TIMESTAMP(timezone=True), "postgresql"
            ),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("end_time > start_time", name="ck_workday_window_positive"),
    )
    op.create_index(
        "ux_work_days_master_date",
        "work_days",
        ["master_id", "work_date"],
        unique=True,
    )
    op.create_index(
        "idx_work_days_master_date_active",
        "work_days",
        ["master_id", "work_date"],
        postgresql_where=sa.text("is_active = TRUE"),
    )

    # 2. Перенос данных из slots: для каждого (master_id, slot_date) — окно
    #    [min(slot_hour), max(slot_hour)+60min]. Python loop для cross-DB time arithmetic
    #    (Postgres make_time() и SQLite time() строки — различаются; Python универсальнее).
    #    Note: op.bulk_insert через sa.table с типизированными sa.Column (sa.Uuid, sa.Date)
    #    на SQLite не покрыт pytest (тесты в tests/test_workday.py переиграют логику через
    #    ORM session.add, не через op.bulk_insert). Smoke-test на Postgres-копии prod базы
    #    (PLANS.md:203) покрывает prod-path. SQLite path проверяется через dev-режим
    #    `alembic upgrade head` вручную (если dev-база — SQLite).
    rows = bind.execute(
        sa.text(
            "SELECT master_id, slot_date, "
            "MIN(slot_hour) AS min_h, MAX(slot_hour) AS max_h "
            "FROM slots GROUP BY master_id, slot_date"
        )
    ).fetchall()

    work_days_to_insert = []
    for master_id, slot_date, min_h, max_h in rows:
        start_time = time(min_h, 0)
        # Буфер +60мин (НЕ +30мин) — покрывает услуги длительностью до 60 мин.
        # Booking.end_at = slot.slot_hour + service.duration_minutes, для 60-мин
        # услуги на slot=16 → booking до 17:00. WorkDay.end_time должен покрывать
        # ВСЕ существующие bookings, иначе 5.3 invariants (Booking.end_at <=
        # WorkDay.end_time) сделают их невалидными.
        # Если есть услуги >60 мин — data-fix в 5.3 (расширение WorkDay.end_time).
        # max_h=23 → 23:00 + 60мин = 23:30 (clamped через datetime — 23:00+60 = next day,
        # берём .time() что обрезает до time — НЕ midnight overflow-safe).
        # Для slot 23:00 это edge: end_dt = 00:00 next day, .time() = 00:00 < start_time
        # → CheckConstraint violation. Поэтому для max_h=23 используем +30мин (slot
        # длительностью 30мин — нормальный случай), а не +60мин.
        if max_h == 23:
            end_dt = datetime(2000, 1, 1, max_h, 0, tzinfo=UTC) + timedelta(minutes=30)
        else:
            end_dt = datetime(2000, 1, 1, max_h, 0, tzinfo=UTC) + timedelta(minutes=60)
        work_days_to_insert.append(
            {
                "id": uuid.uuid4(),
                "master_id": master_id,
                "work_date": slot_date,
                "start_time": start_time,
                "end_time": end_dt.time(),
                "max_concurrent_clients": 1,
                "is_active": True,
            }
        )

    if work_days_to_insert:
        work_days_table = sa.table(
            "work_days",
            sa.Column("id", sa.Uuid),
            sa.Column("master_id", sa.Uuid),
            sa.Column("work_date", sa.Date),
            sa.Column("start_time", sa.Time),
            sa.Column("end_time", sa.Time),
            sa.Column("max_concurrent_clients", sa.Integer),
            sa.Column("is_active", sa.Boolean),
        )
        op.bulk_insert(work_days_table, work_days_to_insert)

    # 3. DROP EXCLUDE constraint на Postgres (ломает multi-client в 5.5)
    #    На SQLite — no-op (002 был no-op, UNIQUE(slot_id) на bookings остаётся до 006)
    if dialect == "postgresql":
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS no_overlap")


def downgrade() -> None:
    op.drop_index("idx_work_days_master_date_active", table_name="work_days")
    op.drop_index("ux_work_days_master_date", table_name="work_days")
    op.drop_table("work_days")
    # EXCLUDE constraint no_overlap НЕ восстанавливается — multi-client (5.5) требует
    # его отсутствия. Обратный перенос WorkDay→Slot не тривиален (slot_hour из диапазона
    # требует reverse инженерии). Миграция 005 объявлена one-way в проде после smoke-test.
