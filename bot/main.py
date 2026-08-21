"""Bot entry point — Bot, Dispatcher, scheduler lifecycle, polling.

Wiring order (spec.md 236, 358-382):
  1. Build global scheduler (module level — single instance per process)
  2. Bot + Dispatcher setup
  3. Inject scheduler into workflow_data (dp["scheduler"]) — aiogram 3.x pattern
  4. Register routers: start_router first (so /start caught before /book), client_router second
  5. Register SessionTimeoutMiddleware on BOTH message + callback_query
     (works on text in entering_name/entering_service AND inline buttons in
     selecting_date/selecting_slot/confirming)
  6. on_startup: scheduler.start() (sync) → on_startup_scan (requires started scheduler)
  7. on_shutdown: scheduler.shutdown(wait=False) (sync)
  8. start_polling
"""

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scheduler import build_scheduler, on_startup_scan

from bot.config import get_settings
from bot.db import async_session_factory
from bot.handlers.admin import router as admin_router
from bot.handlers.client import router as client_router
from bot.handlers.start import router as start_router
from bot.middlewares.session_timeout import SessionTimeoutMiddleware
from bot.session import CorpAiohttpSession

logger = logging.getLogger(__name__)

# Global scheduler — single instance per process (spec.md 341-349).
# Module-level so handlers can inject it via dp["scheduler"] workflow_data.
scheduler = build_scheduler()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def _on_startup(bot: Bot, scheduler: AsyncIOScheduler) -> None:
    """on_startup — start scheduler (sync) then rescan bookings.

    Order matters: on_startup_scan calls schedule_for_booking which calls
    scheduler.add_job. add_job works on unstarted scheduler (job stored but
    not scheduled), but for jobs to fire scheduler must be started first.
    """
    # AsyncIOScheduler.start() is sync (APScheduler 3.x — not a coroutine)
    scheduler.start()
    # on_startup_scan — async, requires started scheduler for jobs to be
    # scheduled immediately (Phase 1: overdue reminders, Phase 2: upcoming)
    await on_startup_scan(scheduler, async_session_factory, bot)
    logger.info("Scheduler started, on_startup_scan complete")


async def _on_shutdown(bot: Bot, scheduler: AsyncIOScheduler) -> None:
    """on_shutdown — stop scheduler (sync, no wait — bot is shutting down)."""
    # AsyncIOScheduler.shutdown() is sync (APScheduler 3.x — not a coroutine)
    scheduler.shutdown(wait=False)
    logger.info("Scheduler shut down")


async def main() -> None:
    settings = get_settings()
    setup_logging()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=CorpAiohttpSession(),
    )
    dp = Dispatcher()

    # Inject scheduler into workflow_data — handlers receive it as kwarg
    # `scheduler: AsyncIOScheduler` via aiogram's _prepare_kwargs filter.
    dp["scheduler"] = scheduler

    # Register routers — order: start_router first (/start caught before /book),
    # admin_router second (master commands: /addslots /closeslot /today /week /services),
    # client_router last (booking flow + /cancel + fallback).
    # /cancel registered in client_router catches StateFilter("*") — works for admin too
    # because admin commands run StateFilter(None) — no state to cancel.
    dp.include_router(start_router)
    dp.include_router(admin_router)
    dp.include_router(client_router)

    # Register FSM timeout middleware on BOTH message + callback_query.
    # Works on text input (entering_name, entering_service) AND inline
    # button taps (selecting_date, selecting_slot, confirming).
    dp.message.middleware(SessionTimeoutMiddleware())
    dp.callback_query.middleware(SessionTimeoutMiddleware())

    # Lifecycle hooks — scheduler start/shutdown
    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    try:
        logger.info("Polling started")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
