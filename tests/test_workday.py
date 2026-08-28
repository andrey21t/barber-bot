"""Tests for WorkDay model + Slot→WorkDay migration logic (Этап 5.2).

Two test groups:
1. test_workday_* — WorkDay model constraints (UNIQUE, CheckConstraint, defaults)
2. test_migration_005_* — Slot→WorkDay data transfer logic.
   Executes the same SQL/Python logic that мигration 005 uses, on in-memory SQLite.
   Migration itself (alembic upgrade head) — smoke-test on dev-copy of prod DB
   (PLANS.md:203, one-way door). pytest тестирует логику переноса, не alembic runtime.

Baseline: 265 → 274 after 5.2 (+9 tests: 4 model + 5 migration logic).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from bot.models import Business, Master, Slot, WorkDay
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_master(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper — create business + master, return (business_id, master_id)."""
    biz = Business(name="Test", telegram_owner_id=461355056, timezone="Europe/Moscow")
    session.add(biz)
    await session.flush()
    master = Master(business_id=biz.id, name="Екатерина", telegram_id=461355056, role="owner")
    session.add(master)
    await session.flush()
    return biz.id, master.id


# ---------------------------------------------------------------------------
# WorkDay model tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workday_create_minimal(session: AsyncSession, tomorrow_date: date) -> None:
    """Create WorkDay with required fields — defaults applied."""
    _, master_id = await _seed_master(session)
    wd = WorkDay(
        master_id=master_id,
        work_date=tomorrow_date,
        start_time=time(11, 0),
        end_time=time(18, 0),
    )
    session.add(wd)
    await session.commit()

    assert wd.max_concurrent_clients == 1  # default
    assert wd.is_active is True  # default
    assert wd.id is not None
    assert wd.created_at is not None


@pytest.mark.asyncio
async def test_workday_unique_master_date(session: AsyncSession, tomorrow_date: date) -> None:
    """UNIQUE(master_id, work_date) — duplicate raises IntegrityError."""
    _, master_id = await _seed_master(session)
    wd1 = WorkDay(
        master_id=master_id, work_date=tomorrow_date, start_time=time(11), end_time=time(18)
    )
    wd2 = WorkDay(
        master_id=master_id, work_date=tomorrow_date, start_time=time(12), end_time=time(20)
    )
    session.add_all([wd1, wd2])
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_workday_check_constraint_end_after_start(
    session: AsyncSession, tomorrow_date: date
) -> None:
    """CheckConstraint end_time > start_time — equal times raise IntegrityError."""
    _, master_id = await _seed_master(session)
    wd = WorkDay(
        master_id=master_id,
        work_date=tomorrow_date,
        start_time=time(14, 0),
        end_time=time(14, 0),  # equal → violation
    )
    session.add(wd)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_workday_multi_client_capacity_2(session: AsyncSession, tomorrow_date: date) -> None:
    """WorkDay.max_concurrent_clients=2 — multi-client flag (5.5 prep)."""
    _, master_id = await _seed_master(session)
    wd = WorkDay(
        master_id=master_id,
        work_date=tomorrow_date,
        start_time=time(11),
        end_time=time(18),
        max_concurrent_clients=2,
    )
    session.add(wd)
    await session.commit()
    assert wd.max_concurrent_clients == 2


# ---------------------------------------------------------------------------
# Slot → WorkDay migration logic (005)
# ---------------------------------------------------------------------------


async def _run_migration_logic(session: AsyncSession) -> list[WorkDay]:
    """Replays мигration 005 SQL/Python logic on test engine.

    Production runs this via alembic; here we replay the same SELECT/INSERT
    against the test session to verify transfer correctness on in-memory SQLite.
    """
    rows = (
        await session.execute(
            text(
                "SELECT master_id, slot_date, "
                "MIN(slot_hour) AS min_h, MAX(slot_hour) AS max_h "
                "FROM slots GROUP BY master_id, slot_date"
            )
        )
    ).fetchall()
    work_days: list[WorkDay] = []
    for master_id_raw, slot_date_raw, min_h, max_h in rows:
        # SELECT через text() на SQLite возвращает UUID/Date как str (без type
        # processor). Конвертируем вручную. На Postgres asyncpg возвращает uuid.UUID
        # и datetime.date — isinstance(...) сработает корректно. В реальной миграции
        # 005 SQLAlchemy делает это автоматически через sa.Column types в sa.table.
        master_id = (
            master_id_raw if isinstance(master_id_raw, uuid.UUID) else uuid.UUID(str(master_id_raw))
        )
        slot_date = (
            slot_date_raw
            if isinstance(slot_date_raw, date)
            else date.fromisoformat(str(slot_date_raw))
        )
        start_time = time(min_h, 0)
        # Буфер +60мин для покрытия 60-мин услуг (см. коммент в миграции 005).
        # Edge: max_h=23 → +30мин (чтобы не перейти в полночь → CheckConstraint violation).
        if max_h == 23:
            end_dt = datetime(2000, 1, 1, max_h, 0, tzinfo=UTC) + timedelta(minutes=30)
        else:
            end_dt = datetime(2000, 1, 1, max_h, 0, tzinfo=UTC) + timedelta(minutes=60)
        wd = WorkDay(
            master_id=master_id,
            work_date=slot_date,
            start_time=start_time,
            end_time=end_dt.time(),
            max_concurrent_clients=1,
            is_active=True,
        )
        session.add(wd)
        work_days.append(wd)
    await session.commit()
    return work_days


@pytest.mark.asyncio
async def test_migration_005_single_slot_per_day(
    session: AsyncSession, tomorrow_date: date
) -> None:
    """1 slot at 14:00 → workday window [14:00, 15:00] (max=14, +60min buffer)."""
    _, master_id = await _seed_master(session)
    session.add(Slot(master_id=master_id, slot_date=tomorrow_date, slot_hour=14, status="open"))
    await session.commit()

    work_days = await _run_migration_logic(session)

    assert len(work_days) == 1
    wd = work_days[0]
    assert wd.master_id == master_id
    assert wd.work_date == tomorrow_date
    assert wd.start_time == time(14, 0)
    assert wd.end_time == time(15, 0)  # 14:00 + 60min buffer
    assert wd.max_concurrent_clients == 1


@pytest.mark.asyncio
async def test_migration_005_multiple_slots_per_day(
    session: AsyncSession, tomorrow_date: date
) -> None:
    """3 slots at 11,12,13 → workday window [11:00, 14:00] (max=13, +60min=14:00)."""
    _, master_id = await _seed_master(session)
    for hour in (11, 12, 13):
        session.add(
            Slot(master_id=master_id, slot_date=tomorrow_date, slot_hour=hour, status="open")
        )
    await session.commit()

    work_days = await _run_migration_logic(session)

    assert len(work_days) == 1
    wd = work_days[0]
    assert wd.start_time == time(11, 0)  # MIN(slot_hour)=11
    assert wd.end_time == time(14, 0)  # MAX(slot_hour)=13, +60min buffer = 14:00


@pytest.mark.asyncio
async def test_migration_005_multiple_days(session: AsyncSession, tomorrow_date: date) -> None:
    """Slots on 2 different dates → 2 separate workdays."""
    _, master_id = await _seed_master(session)
    day2 = tomorrow_date + timedelta(days=1)
    session.add(Slot(master_id=master_id, slot_date=tomorrow_date, slot_hour=11, status="open"))
    session.add(Slot(master_id=master_id, slot_date=day2, slot_hour=15, status="open"))
    await session.commit()

    work_days = await _run_migration_logic(session)

    assert len(work_days) == 2
    by_date = {wd.work_date: wd for wd in work_days}
    assert by_date[tomorrow_date].start_time == time(11, 0)
    assert by_date[tomorrow_date].end_time == time(12, 0)  # 11:00 + 60min
    assert by_date[day2].start_time == time(15, 0)
    assert by_date[day2].end_time == time(16, 0)  # 15:00 + 60min


@pytest.mark.asyncio
async def test_migration_005_no_slots(session: AsyncSession) -> None:
    """No slots in DB → no workdays inserted (defensive — empty migration OK)."""
    await _seed_master(session)  # master exists but no slots
    await session.commit()

    work_days = await _run_migration_logic(session)

    assert work_days == []


@pytest.mark.asyncio
async def test_migration_005_max_hour_23_no_overflow(
    session: AsyncSession, tomorrow_date: date
) -> None:
    """Edge: slot at 23:00 → end_time 23:30 (special case: +30min to avoid midnight overflow)."""
    _, master_id = await _seed_master(session)
    session.add(Slot(master_id=master_id, slot_date=tomorrow_date, slot_hour=23, status="open"))
    await session.commit()

    work_days = await _run_migration_logic(session)

    assert len(work_days) == 1
    wd = work_days[0]
    assert wd.start_time == time(23, 0)
    assert wd.end_time == time(23, 30)  # NOT 00:30 next day
