"""Notifications — Pure service layer.

Contract:
- send_master_new_text: builds text for master notification (Pure, no Telegram API)
- log_notification: idempotent INSERT to notifications_log (UNIQUE(booking_id, kind) guard)
- on_startup_scan: 2 phases — fire_overdue_reminders + schedule_for_booking for ALL upcoming
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Booking, NotificationLog


async def log_notification(session: AsyncSession, booking_id: UUID, kind: str) -> bool:
    """Idempotent INSERT to notifications_log.

    Returns True if newly inserted, False if already logged (UNIQUE guard caught).
    """
    entry = NotificationLog(booking_id=booking_id, kind=kind)
    session.add(entry)
    try:
        await session.flush()
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def get_overdue_bookings_without_remind_24h(
    session: AsyncSession, now: datetime
) -> list[Booking]:
    """Phase 1 of on_startup_scan: bookings that missed remind_24h while bot was down.

    Spec.md 363-372: status='confirmed' AND start_at in (now-24h, now) AND
    NOT EXISTS notifications_log(booking_id, kind='remind_24h').

    L3 fix (Session 4, code-review S suggestion): single query with NOT EXISTS
    subquery instead of N+1 (was 1 SELECT for bookings + 1 SELECT per booking
    for NotificationLog = N+1 queries; now 1 query).
    """
    window_start = now - timedelta(hours=24)
    # Single query with correlated NOT EXISTS subquery — DB does the filtering
    remind_24h_exists = (
        select(NotificationLog.id)
        .where(
            NotificationLog.booking_id == Booking.id,
            NotificationLog.kind == "remind_24h",
        )
        .exists()
    )
    stmt = select(Booking).where(
        Booking.status == "confirmed",
        Booking.start_at > window_start,
        Booking.start_at < now,
        ~remind_24h_exists,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_upcoming_bookings_for_reschedule(
    session: AsyncSession, now: datetime, look_ahead_hours: int = 25
) -> list[Booking]:
    """Phase 2 of on_startup_scan: schedule_for_booking for ALL upcoming.

    Spec.md 374-380: start_at > now AND start_at < now + 25h AND status='confirmed'.
    Limited to 25h window to avoid MemoryJobStore overflow (job per booking).
    """
    window_end = now + timedelta(hours=look_ahead_hours)
    stmt = select(Booking).where(
        Booking.status == "confirmed",
        Booking.start_at > now,
        Booking.start_at < window_end,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
