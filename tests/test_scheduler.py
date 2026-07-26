"""Tests for scheduler.schedule_for_booking + on_startup_scan.

Coverage:
- Acceptance #3: scheduler.get_jobs() returns 2 (remind_24h + remind_1h)
- on_startup_scan Phase 1: fire_overdue_reminders
- on_startup_scan Phase 2: schedule_for_booking for upcoming bookings
- job_id format deterministic: f"remind_24h_{booking_id}"
- replace_existing=True — idempotent (requires scheduler.start() to apply)
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.models import Booking, Business, Client, Master, Slot
from scheduler import (
    build_scheduler,
    on_startup_scan,
    remove_jobs_for_booking,
    schedule_for_booking,
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def scheduler() -> AsyncIOScheduler:
    """Fresh scheduler per test (not started)."""
    return build_scheduler()


def _start_scheduler(s: AsyncIOScheduler) -> None:
    """Helper: start scheduler paused (jobs don't fire but state ops work).

    AsyncIOScheduler.start() is NOT a coroutine in APScheduler 3.x — verified.
    """
    s.start(paused=True)


async def _seed_booking(session: AsyncSession, start_at: datetime) -> Booking:
    """Seed a confirmed booking with given start_at (UTC)."""
    biz = Business(name="Test", telegram_owner_id=461355056, timezone="Europe/Moscow")
    session.add(biz)
    await session.flush()

    master = Master(business_id=biz.id, name="Екатерина", telegram_id=461355056)
    client = Client(telegram_id=111222333)
    session.add_all([master, client])
    await session.flush()

    slot = Slot(
        master_id=master.id, slot_date=start_at.date(), slot_hour=14, status="booked"
    )
    session.add(slot)
    await session.flush()

    booking = Booking(
        slot_id=slot.id,
        business_id=biz.id,
        master_id=master.id,
        client_id=client.id,
        service_id=None,
        service_title_snapshot="Test",
        client_name_snapshot="Test",
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        status="confirmed",
    )
    session.add(booking)
    await session.commit()
    return booking


@pytest.mark.asyncio
async def test_build_scheduler_memory_jobstore(scheduler: AsyncIOScheduler) -> None:
    """Scheduler uses MemoryJobStore (dev config)."""
    from apscheduler.jobstores.memory import MemoryJobStore

    assert "default" in scheduler._jobstores
    assert isinstance(scheduler._jobstores["default"], MemoryJobStore)


@pytest.mark.asyncio
async def test_schedule_for_booking_creates_two_jobs(scheduler: AsyncIOScheduler) -> None:
    """Acceptance #3: scheduler.get_jobs() returns 2 (remind_24h + remind_1h)."""
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(days=2)

    schedule_for_booking(scheduler, booking_id, start_at)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 2

    job_ids = {j.id for j in jobs}
    assert f"remind_24h_{booking_id}" in job_ids
    assert f"remind_1h_{booking_id}" in job_ids

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_schedule_for_booking_replace_existing(scheduler: AsyncIOScheduler) -> None:
    """schedule_for_booking is idempotent — calling twice doesn't duplicate jobs."""
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(days=2)

    schedule_for_booking(scheduler, booking_id, start_at)
    schedule_for_booking(scheduler, booking_id, start_at)  # idempotent

    assert len(scheduler.get_jobs()) == 2

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_remove_jobs_for_booking_removes_both(scheduler: AsyncIOScheduler) -> None:
    """remove_jobs_for_booking removes both remind_24h and remind_1h."""
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(days=2)

    schedule_for_booking(scheduler, booking_id, start_at)
    assert len(scheduler.get_jobs()) == 2

    remove_jobs_for_booking(scheduler, booking_id)
    assert len(scheduler.get_jobs()) == 0

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_remove_jobs_for_booking_idempotent(scheduler: AsyncIOScheduler) -> None:
    """remove_jobs_for_booking is idempotent — no error if jobs already removed."""
    _start_scheduler(scheduler)

    booking_id = uuid4()
    remove_jobs_for_booking(scheduler, booking_id)  # should not raise
    assert len(scheduler.get_jobs()) == 0

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_schedule_for_booking_run_date_is_24h_before(
    scheduler: AsyncIOScheduler,
) -> None:
    """remind_24h job runs at start_at - 24h, remind_1h at start_at - 1h."""
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(days=2)

    schedule_for_booking(scheduler, booking_id, start_at)

    job_24h = scheduler.get_job(f"remind_24h_{booking_id}")
    job_1h = scheduler.get_job(f"remind_1h_{booking_id}")

    assert job_24h is not None
    assert job_1h is not None

    expected_24h = start_at - timedelta(hours=24)
    expected_1h = start_at - timedelta(hours=1)
    actual_24h = job_24h.trigger.get_next_fire_time(None, datetime.now(UTC))
    actual_1h = job_1h.trigger.get_next_fire_time(None, datetime.now(UTC))
    assert abs((actual_24h - expected_24h).total_seconds()) < 60
    assert abs((actual_1h - expected_1h).total_seconds()) < 60

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_on_startup_scan_phase_2_reschedules_upcoming(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """on_startup_scan Phase 2: schedule_for_booking for ALL upcoming bookings."""
    _start_scheduler(scheduler)

    # Tomorrow at 14:00 Moscow → 11:00 UTC
    tomorrow_date = (datetime.now(UTC) + timedelta(days=1)).date()
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    start_at = datetime.combine(
        tomorrow_date, dtime(hour=14), tzinfo=ZoneInfo("Europe/Moscow")
    ).astimezone(UTC)

    booking = await _seed_booking(session, start_at)

    # Run on_startup_scan
    await on_startup_scan(scheduler, session_factory)

    # Phase 2 should have scheduled 2 jobs for this booking
    jobs = scheduler.get_jobs()
    assert len(jobs) == 2
    assert f"remind_24h_{booking.id}" in {j.id for j in jobs}

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_on_startup_scan_phase_1_logs_overdue(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """on_startup_scan Phase 1: log_notification for overdue bookings without remind_24h."""
    _start_scheduler(scheduler)

    from bot.models import NotificationLog
    from sqlalchemy import select

    # Booking 12h ago (within 24h window)
    overdue_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, overdue_start)

    # Run on_startup_scan
    await on_startup_scan(scheduler, session_factory)

    # Phase 1 should have logged remind_24h for overdue booking
    stmt = select(NotificationLog).where(
        NotificationLog.booking_id == booking.id,
        NotificationLog.kind == "remind_24h",
    )
    log = (await session.execute(stmt)).scalar_one_or_none()
    assert log is not None

    scheduler.shutdown(wait=False)
