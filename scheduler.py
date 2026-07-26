"""Scheduler — APScheduler AsyncIOScheduler + MemoryJobStore (dev).

Spec.md 322-393, 354:
- MemoryJobStore in dev SQLite (no pickle)
- on_startup_scan пересоздаёт jobs при restart
- schedule_for_booking: add_job remind_24h + remind_1h (replace_existing=True)
- job_id format: f"remind_24h_{booking_id}", f"remind_1h_{booking_id}" (deterministic for replace)
- misfire_grace_time=3600 (1h — Render sleep 15min = 900s → 3600s запас)
- coalesce=True, max_instances=1
"""

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.config import get_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_scheduler() -> AsyncIOScheduler:
    """Build AsyncIOScheduler with dev config (MemoryJobStore).

    In prod (Урок 2.6): swap MemoryJobStore → SQLAlchemyJobStore(engine=sync_engine).
    """
    settings = get_settings()
    return AsyncIOScheduler(
        timezone=ZoneInfo(settings.TIMEZONE),
        jobstores={"default": MemoryJobStore()},
        job_defaults={
            "coalesce": True,
            "misfire_grace_time": settings.MISFIRE_GRACE_TIME,
            "max_instances": 1,
        },
    )


async def send_reminder(booking_id: UUID, kind: str, bot: Any = None) -> None:
    """Job target — sends reminder to client.

    Stub for Шаг 3: real implementation in Урок 2.4 (master handlers).
    Real flow:
      1. SELECT booking
      2. INSERT notifications_log(booking_id, kind) — UNIQUE guard
      3. bot.send_message(client_id, "Напоминание: ...")
    """
    # TODO Урок 2.4: real send_reminder with DB lookup + bot.send_message
    return None


def schedule_for_booking(
    scheduler: AsyncIOScheduler,
    booking_id: UUID,
    start_at: datetime,
) -> None:
    """Schedule remind_24h + remind_1h for a booking.

    replace_existing=True — idempotent, safe to call multiple times.
    If start_at - 24h is in the past, APScheduler's misfire_grace_time handles it
    (job fires immediately if within grace window, otherwise dropped — on_startup_scan catches).
    """
    settings = get_settings()
    remind_24h_at = start_at - timedelta(hours=settings.REMINDER_24H_BEFORE)
    remind_1h_at = start_at - timedelta(hours=settings.REMINDER_1H_BEFORE)

    job_id_24h = f"remind_24h_{booking_id}"
    job_id_1h = f"remind_1h_{booking_id}"

    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=remind_24h_at,
        args=[booking_id, "remind_24h"],
        id=job_id_24h,
        replace_existing=True,
    )
    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=remind_1h_at,
        args=[booking_id, "remind_1h"],
        id=job_id_1h,
        replace_existing=True,
    )


def remove_jobs_for_booking(scheduler: AsyncIOScheduler, booking_id: UUID) -> None:
    """Remove remind_24h + remind_1h for a booking (on cancel)."""
    for kind in ("remind_24h", "remind_1h"):
        job_id = f"{kind}_{booking_id}"
        with suppress(Exception):
            scheduler.remove_job(job_id)


async def on_startup_scan(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Any = None,
) -> None:
    """on_startup — 2 phases (spec.md 358-382):

    Phase 1: fire_overdue_reminders — bookings без remind_24h где start_at в окне (now-24h, now)
    Phase 2: schedule_for_booking для ВСЕХ upcoming (start_at > now, < now+25h)
    """
    from bot.services.notifications import (
        get_overdue_bookings_without_remind_24h,
        get_upcoming_bookings_for_reschedule,
        log_notification,
    )

    now = datetime.now(UTC)

    async with session_factory() as session:
        # Phase 1: fire overdue (stub — real send in Урок 2.4)
        overdue = await get_overdue_bookings_without_remind_24h(session, now)
        for b in overdue:
            # TODO Урок 2.4: actually send message via bot
            await log_notification(session, b.id, "remind_24h")

        # Phase 2: reschedule upcoming
        upcoming = await get_upcoming_bookings_for_reschedule(session, now, look_ahead_hours=25)
        for b in upcoming:
            schedule_for_booking(scheduler, b.id, b.start_at)
