"""Scheduler — APScheduler AsyncIOScheduler with cross-DB jobstore.

Spec.md 322-393, 354:
- Dev (SQLite): MemoryJobStore (no pickle, no sync engine)
- Prod (Postgres): SQLAlchemyJobStore(engine=sync_engine) — sync psycopg2 engine
  (pickle-сериализация не работает с asyncpg; separate sync engine required).
- on_startup_scan пересоздаёт jobs при restart (BB-012)
- schedule_for_booking: add_job remind_24h + remind_1h (replace_existing=True)
- job_id format: f"remind_24h_{booking_id}", f"remind_1h_{booking_id}" (deterministic for replace)
- misfire_grace_time=3600 (1h — Render sleep 15min = 900s → 3600s запас)
- coalesce=True, max_instances=1

SQLAlchemyJobStore.__init__ lazy (verified apscheduler/jobstores/sqlalchemy.py:65-85):
engine stored as reference, NO connection на construct. Table `apscheduler_jobs`
created at `scheduler.start()` via `jobs_t.create(engine, True)` (CREATE IF NOT EXISTS).
Module-level `scheduler = build_scheduler()` безопасен.
"""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.config import get_settings
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# Global bot ref — set by main.py:_on_startup BEFORE scheduler.start().
# Anti-pattern (global mutable state), but aiogram Bot is not picklable (aiohttp
# session) → cannot be passed via APScheduler args (pickle). Scheduled jobs call
# send_reminder(booking_id, kind) — bot defaults to None → falls back to _bot_ref.
# Direct callers (on_startup_scan) pass bot explicitly. Session 4: refactor via
# context var / DI.
_bot_ref: Any = None


def _set_bot_ref(bot: Any) -> None:
    """Set global bot ref. Must be called from main.py:_on_startup BEFORE
    scheduler.start() so scheduled jobs can send messages.
    """
    global _bot_ref
    _bot_ref = bot


def build_scheduler() -> AsyncIOScheduler:
    """Build AsyncIOScheduler with cross-DB jobstore branching.

    Dev (DATABASE_URL=sqlite, DATABASE_URL_SYNC empty) → MemoryJobStore.
    Prod (DATABASE_URL=postgresql, DATABASE_URL_SYNC empty → derived) → SQLAlchemyJobStore.
    """
    settings = get_settings()
    sync_url = settings.sync_database_url
    # SQLAlchemyJobStore.__init__ lazy — engine не подключается на construct
    # (verified apscheduler/jobstores/sqlalchemy.py:65-85). Module-level
    # build_scheduler() безопасен — actual table CREATE в scheduler.start().
    if sync_url.startswith("postgresql"):
        sync_engine = create_engine(
            sync_url,
            pool_pre_ping=True,  # detect dead conn после Render sleep 15 мин
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
        )
        jobstore = SQLAlchemyJobStore(engine=sync_engine)
    else:
        # Dev SQLite — MemoryJobStore (no pickle, no sync engine)
        jobstore = MemoryJobStore()
    return AsyncIOScheduler(
        timezone=ZoneInfo(settings.TIMEZONE),
        jobstores={"default": jobstore},
        job_defaults={
            "coalesce": True,
            "misfire_grace_time": settings.MISFIRE_GRACE_TIME,
            "max_instances": 1,
        },
    )


async def send_reminder(booking_id: UUID, kind: str, bot: Any = None) -> None:
    """Job target — sends reminder to client.

    ⚠️ PICKLE-STABLE SIGNATURE — DO NOT rename/remove args without
    drop+reschedule strategy. SQLAlchemyJobStore pickles (booking_id, kind)
    into apscheduler_jobs.job_state column. `bot` is NOT in scheduled job args
    (see schedule_for_booking: `args=[booking_id, kind]`) — bot is not picklable
    (aiohttp session). Scheduled jobs use global `_bot_ref` fallback; direct
    callers (on_startup_scan) pass bot explicitly. Adding args with defaults is
    safe; renaming/removing is a ONE-WAY DOOR. Migration: scheduler.remove_all_jobs()
    then on_startup_scan reschedules from DB.

    Flow (spec.md 322-326, 358-396):
      1. Resolve bot (param > _bot_ref). None → return (startup not complete).
      2. SELECT booking + client + business (selectinload, single query).
      3. log_notification UNIQUE guard — False = already sent, return.
      4. Build text per kind:
         - remind_24h: "Напоминаю: завтра в 14:00" (local time in business.timezone)
         - remind_1h: "Через час: 14:00"
      5. bot.send_message with try/except:
         - TelegramRetryAfter → asyncio.sleep(retry_after) + retry once
         - TelegramForbiddenError / TelegramBadRequest → log warning, return
         - Other TelegramAPIError → log error, return

    MVP-допущение (spec.md 396): bot crash after INSERT notifications_log but
    before send_message → message lost. Known limitation, prod = two-phase
    (INSERT with sent_at=NULL, UPDATE after send + reaper).
    """
    from aiogram.exceptions import (
        TelegramAPIError,
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramRetryAfter,
    )
    from bot.db import async_session_factory
    from bot.services.notifications import log_notification
    from sqlalchemy import select

    active_bot = bot if bot is not None else _bot_ref
    if active_bot is None:
        logger.warning(
            "send_reminder: bot unavailable (startup not complete?), skip booking=%s kind=%s",
            booking_id,
            kind,
        )
        return

    async with async_session_factory() as session:
        # Booking has no ORM relationships (models.py uses plain FKs without
        # relationship()). Explicit JOIN to fetch Client + Business in one query.
        from bot.models import Booking, Business, Client

        stmt = (
            select(Booking, Client, Business)
            .join(Client, Booking.client_id == Client.id)
            .join(Business, Booking.business_id == Business.id)
            .where(Booking.id == booking_id)
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            logger.warning("send_reminder: booking %s not found", booking_id)
            return
        booking, client, business = row

        # booking.start_at is naive UTC on SQLite (per models.py:30 comment),
        # aware UTC on Postgres. .replace(tzinfo=UTC) is no-op on aware, makes
        # naive → aware UTC before astimezone. Without this, Python interprets
        # naive as system-local TZ (Mac default Europe/Moscow → wrong time;
        # Render TZ=UTC correct by accident). Mirror of booking.py:380, :531, :650.
        #
        # ZoneInfo validation BEFORE log_notification: if timezone is invalid,
        # return BEFORE inserting into notifications_log → retry possible when
        # DBA fixes the timezone value. If we logged first then failed on
        # ZoneInfo, UNIQUE(booking_id, kind) would block retry forever (message
        # permanently lost for this booking+kind).
        try:
            tz = ZoneInfo(business.timezone)
        except KeyError as e:
            # ZoneInfoNotFoundError is subclass of KeyError (PEP 615).
            logger.error(
                "send_reminder: invalid business.timezone=%r (booking=%s): %s",
                business.timezone,
                booking_id,
                e,
            )
            return
        time_str = booking.start_at.replace(tzinfo=UTC).astimezone(tz).strftime("%H:%M")
        if kind == "remind_24h":
            text = f"Напоминаю: завтра в {time_str}"
        elif kind == "remind_1h":
            text = f"Через час: {time_str}"
        else:
            logger.warning("send_reminder: unknown kind=%s for booking=%s, skip", kind, booking_id)
            return

        # log_notification UNIQUE guard AFTER timezone validation — ensures
        # invalid timezone doesn't permanently block retry.
        if not await log_notification(session, booking_id, kind):
            return

        for attempt in range(2):
            try:
                await active_bot.send_message(client.telegram_id, text)
                return
            except TelegramRetryAfter as e:
                if attempt == 0:
                    logger.warning(
                        "send_reminder: flood control, retry after %ss (booking=%s kind=%s)",
                        e.retry_after,
                        booking_id,
                        kind,
                    )
                    await asyncio.sleep(e.retry_after)
                    continue
                logger.error(
                    "send_reminder: flood retry failed (booking=%s kind=%s)",
                    booking_id,
                    kind,
                )
                return
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                logger.warning(
                    "send_reminder: chat issue (booking=%s kind=%s): %s",
                    booking_id,
                    kind,
                    e,
                )
                return
            except TelegramAPIError as e:
                logger.error(
                    "send_reminder: send_message failed (booking=%s kind=%s): %s",
                    booking_id,
                    kind,
                    e,
                )
                return


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

    Phase 1: fire_overdue_reminders — bookings без remind_24h где start_at в окне (now-24h, now).
             Calls send_reminder(booking_id, kind, bot) — bot passed explicitly (UNIQUE guard
             inside send_reminder protects against duplicate sends if scan runs multiple times).
             send_reminder opens its OWN session (separate from this scan session) — booking
             IDs extracted here before close, send happens outside `async with` to avoid
             nesting sessions.
    Phase 2: schedule_for_booking для ВСЕХ upcoming (start_at > now, < now+25h).
             Booking.id + start_at extracted into tuples before close — schedule_for_booking
             is sync, but accesses booking attrs lazily → would DetachedInstanceError after
             session close.
    """
    from bot.services.notifications import (
        get_overdue_bookings_without_remind_24h,
        get_upcoming_bookings_for_reschedule,
    )

    now = datetime.now(UTC)

    async with session_factory() as session:
        overdue = await get_overdue_bookings_without_remind_24h(session, now)
        upcoming = await get_upcoming_bookings_for_reschedule(session, now, look_ahead_hours=25)
        # Extract scalars before session close — schedule_for_booking accesses
        # booking.id + booking.start_at lazily → DetachedInstanceError otherwise.
        overdue_ids = [b.id for b in overdue]
        upcoming_pairs = [(b.id, b.start_at) for b in upcoming]

    # Phase 1: fire overdue outside session — send_reminder opens its own.
    for booking_id in overdue_ids:
        await send_reminder(booking_id, "remind_24h", bot=bot)

    # Phase 2: reschedule upcoming — schedule_for_booking is sync (add_job).
    for booking_id, start_at in upcoming_pairs:
        schedule_for_booking(scheduler, booking_id, start_at)
