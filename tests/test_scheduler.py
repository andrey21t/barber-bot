"""Tests for scheduler.schedule_for_booking + on_startup_scan + send_reminder.

Coverage:
- Acceptance #3: scheduler.get_jobs() returns 2 (remind_24h + remind_1h)
- on_startup_scan Phase 1: fire_overdue_reminders (real send_reminder call)
- on_startup_scan Phase 2: schedule_for_booking for upcoming bookings
- send_reminder: 6 cases (booking not found, UNIQUE guard, bot=None, RetryAfter,
  Forbidden/BadRequest, happy path)
- job_id format deterministic: f"remind_24h_{booking_id}"
- replace_existing=True — idempotent (requires scheduler.start() to apply)
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.models import Booking, Business, Client, Master, Slot
from freezegun import freeze_time
from scheduler import (
    build_scheduler,
    on_startup_scan,
    remove_jobs_for_booking,
    schedule_for_booking,
    send_reminder,
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

    slot = Slot(master_id=master.id, slot_date=start_at.date(), slot_hour=14, status="booked")
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


def test_set_bot_ref_sets_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """_set_bot_ref sets the global _bot_ref for scheduled jobs.

    Called from main.py:_on_startup before scheduler.start() so that
    scheduled jobs (which don't receive bot as arg) can fall back to
    _bot_ref. Regression guard: if someone renames _bot_ref or removes
    _set_bot_ref, scheduled reminders silently lose bot access.

    Uses monkeypatch (auto-restores on teardown even if assertion fails) to
    avoid leaking _bot_ref=MagicMock() to other tests if the assert raises.
    """
    import scheduler as sched_mod

    mock_bot = MagicMock()
    monkeypatch.setattr(sched_mod, "_bot_ref", mock_bot)
    sched_mod._set_bot_ref(mock_bot)
    assert sched_mod._bot_ref is mock_bot


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
async def test_on_startup_scan_phase_1_sends_overdue(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """on_startup_scan Phase 1: send_reminder for overdue bookings without remind_24h.

    Verifies the actual call path: on_startup_scan → send_reminder(bot=bot) →
    bot.send_message(telegram_id, text). Mock bot so no real Telegram API call.
    send_reminder opens its OWN session via `async_session_factory` (production
    bot.db), so we patch scheduler.async_session_factory → test session_factory
    (in-memory SQLite) so booking seeded via `session` fixture is visible.
    """
    _start_scheduler(scheduler)

    from bot.models import NotificationLog
    from sqlalchemy import select

    # Booking 12h ago (within 24h window)
    overdue_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, overdue_start)

    # Mock bot — capture send_message calls
    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await on_startup_scan(scheduler, session_factory, bot=mock_bot)

    # send_reminder should have been called → bot.send_message invoked once
    assert mock_bot.send_message.await_count == 1
    chat_id, text = mock_bot.send_message.await_args.args
    assert chat_id == 111222333  # client.telegram_id from _seed_booking
    # Text format (5.56): "Напоминаю: завтра в HH:MM — 💇 <service>, мастер <name>"
    assert text.startswith("Напоминаю: завтра в ")
    assert "💇 Test" in text
    assert "мастер Екатерина" in text

    # log_notification should have recorded remind_24h (UNIQUE guard inside send_reminder)
    stmt = select(NotificationLog).where(
        NotificationLog.booking_id == booking.id,
        NotificationLog.kind == "remind_24h",
    )
    log = (await session.execute(stmt)).scalar_one_or_none()
    assert log is not None

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_booking_not_found(
    session_factory: Any, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: booking not found in DB → log warning, return (no send)."""
    _start_scheduler(scheduler)
    mock_bot = AsyncMock()

    # Random UUID not in DB
    fake_id = uuid4()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(fake_id, "remind_24h", bot=mock_bot)

    # No send_message call (booking not found)
    assert mock_bot.send_message.await_count == 0
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_unique_guard_skips_duplicate(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: log_notification returns False (already logged) → skip send.

    Setup: pre-insert NotificationLog for the booking → send_reminder's
    log_notification catches UNIQUE → returns False → no send_message.
    """
    _start_scheduler(scheduler)
    from bot.models import NotificationLog

    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)
    # Pre-insert log so UNIQUE guard triggers on second call
    session.add(NotificationLog(booking_id=booking.id, kind="remind_24h"))
    await session.commit()

    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    # No send_message (UNIQUE guard caught duplicate)
    assert mock_bot.send_message.await_count == 0
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_bot_none_returns(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: bot=None + _bot_ref=None → log warning, return (no send).

    Simulates scheduled job fired before main.py:_on_startup set _bot_ref.
    """
    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    # Both bot param and _bot_ref are None — patch session_factory not needed
    # (returns early before DB lookup)
    with patch("scheduler._bot_ref", None):
        await send_reminder(booking.id, "remind_24h", bot=None)

    # No exception, no send_message (early return)
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_retry_after(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: TelegramRetryAfter → asyncio.sleep(retry_after) + retry once."""
    from aiogram.exceptions import TelegramRetryAfter

    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    mock_bot = AsyncMock()
    # TelegramRetryAfter requires (method, message, retry_after) — use MagicMock for method
    method = MagicMock()
    retry_exc = TelegramRetryAfter(method=method, message="flood", retry_after=1)
    mock_bot.send_message.side_effect = [retry_exc, None]

    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
        patch("scheduler.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    # Should have called sleep once (with retry_after=1)
    mock_sleep.assert_awaited_once_with(1)
    # Should have attempted send_message twice (retry)
    assert mock_bot.send_message.await_count == 2
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_forbidden_logs_warning(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: TelegramForbiddenError (chat blocked) → log warning, return."""
    from aiogram.exceptions import TelegramForbiddenError

    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    mock_bot = AsyncMock()
    method = MagicMock()
    mock_bot.send_message.side_effect = TelegramForbiddenError(
        method=method, message="bot blocked by user"
    )

    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    # Single attempt, no retry (Forbidden ≠ RetryAfter)
    assert mock_bot.send_message.await_count == 1
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_happy_path(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: happy path — bot.send_message called with correct text."""
    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    # send_message called once with client.telegram_id and expected text
    assert mock_bot.send_message.await_count == 1
    chat_id, text = mock_bot.send_message.await_args.args
    assert chat_id == 111222333
    # Text format (5.56): "Напоминаю: завтра в HH:MM — 💇 <service>, мастер <name>"
    assert text.startswith("Напоминаю: завтра в ")
    assert "💇 Test" in text
    assert "мастер Екатерина" in text
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_escapes_master_name_html_metachars(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """Regression test for F1 (code-review 5.56): master.name with HTML
    metacharacters (<, >, &) must be html.escape()'d in reminder text.

    Bot's default parse_mode=ParseMode.HTML (main.py:104). Without escape,
    a master name like "A & B" or "<script>" would fail Telegram HTML parse →
    TelegramBadRequest → log_notification UNIQUE(booking_id, kind) blocks retry
    forever → silent reminder loss.

    Fix (5.56): scheduler.py:181 escapes master.name via html.escape(name, quote=False),
    mirroring booking.py:510-511 pattern for client_name_snapshot.

    Test seeds booking with safe master.name="Екатерина" (default _seed_booking),
    then UPDATEs Master.name to "A & B <b>" via direct SQL (simulates future
    /addmaster or DB-side edit with unsafe chars). Asserts the reminder text
    contains the escaped form "A &amp; B &lt;b&gt;" — NOT the raw metacharacters.
    """
    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    # Corrupt master name AFTER seed (direct UPDATE, simulates DB-side edit)
    from bot.models import Master
    from sqlalchemy import update

    # Two unsafe scenarios in one: HTML metacharacters + newline.
    # html.escape(quote=False) handles & < >; .replace("\n", " ") handles newline
    # (mirrors admin.py:881 pattern for client_name_snapshot).
    unsafe_name = "A & B <b>\nSecondLine"
    await session.execute(
        update(Master).where(Master.id == booking.master_id).values(name=unsafe_name)
    )
    await session.commit()

    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    assert mock_bot.send_message.await_count == 1
    _, text = mock_bot.send_message.await_args.args
    # Escaped form — html.escape(name, quote=False) replaces & < > but not " '.
    # Newline replaced with space → single-line reminder (admin.py:881 pattern).
    expected_escaped = "A &amp; B &lt;b&gt; SecondLine"
    assert expected_escaped in text, (
        f"Expected escaped+squashed master name {expected_escaped!r} in reminder, got {text!r}. "
        "If you see raw '& B <b>' — F1 regression: master.name NOT escape'd → "
        "TelegramBadRequest on parse_mode=HTML → silent reminder loss. "
        "If you see '\\n' — W2 regression: newline NOT squashed → multi-line reminder."
    )
    # Ensure raw metacharacters are NOT present (only escaped forms).
    assert "A & B <b>" not in text, (
        f"Raw HTML metacharacters leaked into reminder text: {text!r}"
    )
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_timezone_utc_to_moscow(
    session_factory: Any,
    session: AsyncSession,
    scheduler: AsyncIOScheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for F1 (code-review Session 3): booking.start_at naive UTC
    on SQLite must be explicitly marked UTC before astimezone. Without
    `.replace(tzinfo=UTC)`, Python interprets naive as system-local TZ → wrong
    time on TZ≠UTC systems.

    Setup: booking.start_at = 2026-01-15 11:00 UTC. Business.timezone =
    Europe/Moscow. Expected: text contains "14:00" (11:00 UTC + 3h = 14:00 MSK).

    TZ-independence: monkeypatch system TZ to Europe/Moscow + time.tzset() so
    naive datetime is interpreted as MSK. Without fix: 11:00 MSK → astimezone(MSK)
    = 11:00 → "11:00" (test fails, regression caught). With fix: 11:00 UTC →
    14:00 MSK (test passes). On TZ=UTC systems without monkeypatch — false
    negative (test passes even without fix, since naive 11:00 == 11:00 UTC).
    """
    import sys
    import time

    if sys.platform == "win32":
        pytest.skip("time.tzset() is Unix-only (test for F1 regression)")

    # Force system TZ to Europe/Moscow — without fix, naive 11:00 → MSK → wrong.
    # With fix: replace(tzinfo=UTC) → 11:00 UTC → 14:00 MSK → correct.
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()

    _start_scheduler(scheduler)

    # booking.start_at = 2026-01-15 11:00 UTC, stored as naive on SQLite
    naive_start = datetime(2026, 1, 15, 11, 0)
    booking = await _seed_booking(session, naive_start)

    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    assert mock_bot.send_message.await_count == 1
    _, text = mock_bot.send_message.await_args.args
    # 11:00 UTC → 14:00 Europe/Moscow (UTC+3, no DST in January).
    # Strict match (5.56) verifies both TZ conversion AND the new reminder format
    # ("Напоминаю: завтра в HH:MM — 💇 <service>, мастер <name>").
    assert text == "Напоминаю: завтра в 14:00 — 💇 Test, мастер Екатерина", (
        f"Expected 'Напоминаю: завтра в 14:00 — 💇 Test, мастер Екатерина' "
        f"(11:00 UTC → 14:00 MSK), got {text!r}. "
        "If you see '11:00' — F1 regression: booking.start_at treated as system-local TZ."
    )
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_invalid_timezone_logs_error(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """Regression test for W1 (code-review Session 3): invalid business.timezone
    raises ZoneInfoNotFoundError → caught, log error, return (no send).
    Without try/except, exception bubbles up + log_notification already committed
    → UNIQUE blocks retry forever.
    """
    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)
    # Corrupt business timezone AFTER seed (direct UPDATE)
    from bot.models import Business
    from sqlalchemy import update

    await session.execute(
        update(Business).where(Business.id == booking.business_id).values(timezone="Europe/Moscowg")
    )
    await session.commit()

    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        # Should not raise — ZoneInfoNotFoundError caught internally
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    # No send_message (invalid timezone caught before send)
    assert mock_bot.send_message.await_count == 0
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_on_startup_scan_phase_2_reschedules_upcoming(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """on_startup_scan Phase 2: schedule_for_booking for ALL upcoming bookings.

    Frozen at 2026-01-15T12:00:00Z to make the test deterministic.
    Without freeze, `start_at = tomorrow 14:00 MSK` lands at tomorrow 11:00 UTC, so
    `start_at - now` ranges 23h..25h+ depending on current UTC time. When run in
    UTC 10:00..11:00 window, `start_at - now` >= 25h and booking falls outside the
    `start_at < now + 25h` upcoming filter (notifications.py:80-82) — Phase 2 returns
    empty list, no jobs scheduled, test fails. freezegun pins `now` to 12:00 UTC so
    `start_at - now` = 23h deterministically, always inside the upcoming window.
    """
    with freeze_time("2026-01-15T12:00:00Z"):
        _start_scheduler(scheduler)

        # Tomorrow at 14:00 Moscow → 11:00 UTC (frozen now = 2026-01-15 12:00 UTC)
        tomorrow_date = (datetime.now(UTC) + timedelta(days=1)).date()
        from datetime import time as dtime
        from zoneinfo import ZoneInfo

        start_at = datetime.combine(
            tomorrow_date, dtime(hour=14), tzinfo=ZoneInfo("Europe/Moscow")
        ).astimezone(UTC)

        booking = await _seed_booking(session, start_at)

        # Run on_startup_scan — mock bot so Phase 1 (no overdue) doesn't fail
        mock_bot = AsyncMock()
        with patch("scheduler._bot_ref", mock_bot):
            await on_startup_scan(scheduler, session_factory, bot=mock_bot)

        # Phase 2 should have scheduled 2 jobs for this booking
        jobs = scheduler.get_jobs()
        assert len(jobs) == 2
        assert f"remind_24h_{booking.id}" in {j.id for j in jobs}

        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_schedule_for_booking_skips_past_due_remind_24h(
    scheduler: AsyncIOScheduler,
) -> None:
    """Past-due skip (BB-PAST-DUE): client books 7h before start → remind_24h_at
    is 17h in the past (past > grace=1h) → skip silently. remind_1h_at is 6h in
    the future → add normally. Reproduces the Оля case (booked 7.5h before).
    """
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(hours=7)

    schedule_for_booking(scheduler, booking_id, start_at)

    assert scheduler.get_job(f"remind_24h_{booking_id}") is None
    assert scheduler.get_job(f"remind_1h_{booking_id}") is not None

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_schedule_for_booking_within_grace_keeps_past_due_remind_1h(
    scheduler: AsyncIOScheduler,
) -> None:
    """Borderline grace: client books 30m before start → remind_1h_at is 30m in
    the past. 30m < grace (1h=3600s) → APScheduler fires immediately on add →
    keep the job. remind_24h_at is 23h30m in the past (>> grace) → skip.
    Verifies that the skip threshold matches APScheduler misfire logic (strict
    `past > grace`), not `past >= grace`.
    """
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(minutes=30)

    schedule_for_booking(scheduler, booking_id, start_at)

    assert scheduler.get_job(f"remind_24h_{booking_id}") is None
    assert scheduler.get_job(f"remind_1h_{booking_id}") is not None

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_schedule_for_booking_keeps_both_when_far_future(
    scheduler: AsyncIOScheduler,
) -> None:
    """Regression guard: past-due skip must not affect normal future bookings.
    start_at = now + 25h → both remind_24h (now + 1h) and remind_1h (now + 24h)
    are in the future → both added.
    """
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) + timedelta(hours=25)

    schedule_for_booking(scheduler, booking_id, start_at)

    assert scheduler.get_job(f"remind_24h_{booking_id}") is not None
    assert scheduler.get_job(f"remind_1h_{booking_id}") is not None

    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_remind_1h_text(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: remind_1h text branch — "Через час в HH:MM — 💇 ..., мастер ...".

    Mirror of test_send_reminder_happy_path but with kind="remind_1h" to cover
    the elif branch (scheduler.py:192-193). Critical path: remind_1h fires for
    every booking (1 hour before start), so the text format must be verified
    independently from remind_24h.
    """
    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(booking.id, "remind_1h", bot=mock_bot)

    assert mock_bot.send_message.await_count == 1
    chat_id, text = mock_bot.send_message.await_args.args
    assert chat_id == 111222333
    # Text format (5.56): "Через час в HH:MM — 💇 <service>, мастер <name>"
    assert text.startswith("Через час в ")
    assert "💇 Test" in text
    assert "мастер Екатерина" in text
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_unknown_kind_skips(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler
) -> None:
    """send_reminder: unknown kind (e.g. "remind_week") → log warning, return.

    Covers defensive else branch (scheduler.py:195-196). Returns BEFORE
    log_notification → UNIQUE(booking_id, kind) not poisoned → retry with
    correct kind still works. Without this guard, a typo in kind would
    silently INSERT into notifications_log and block valid retries.
    """
    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    mock_bot = AsyncMock()
    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
        patch("bot.services.notifications.log_notification", new_callable=AsyncMock) as mock_log,
    ):
        await send_reminder(booking.id, "remind_week", bot=mock_bot)

    # No send_message (unknown kind returns before send + before log_notification)
    assert mock_bot.send_message.await_count == 0
    # log_notification NOT called — UNIQUE(booking_id, kind) not poisoned,
    # retry with correct kind still works. This is the invariant the test
    # guards: if a future refactor moves log_notification before the kind
    # dispatch, send_message.await_count would still be 0 but mock_log would
    # be awaited → test fails → regression caught.
    mock_log.assert_not_awaited()
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_retry_after_exhausted(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """send_reminder: both attempts fail with TelegramRetryAfter → log error, return.

    Covers retry-exhausted path (scheduler.py:217-222). Mirror of
    test_send_reminder_retry_after, but both attempts raise RetryAfter.
    Attempt 0: sleep + continue. Attempt 1: `if attempt == 0` False → log error
    + return (no 3rd try). Real scenario: Telegram under sustained flood control.

    caplog assertion distinguishes this branch (logger.error at line 217,
    "flood retry failed") from the attempt-0 warning (logger.warning at line
    209, "flood control, retry after"). Without caplog, both branches produce
    await_count==2 + sleep called once — the test couldn't tell which log path
    ran.
    """
    from aiogram.exceptions import TelegramRetryAfter

    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    mock_bot = AsyncMock()
    method = MagicMock()
    retry_exc = TelegramRetryAfter(method=method, message="flood", retry_after=1)
    mock_bot.send_message.side_effect = [retry_exc, retry_exc]

    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
        patch("scheduler.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    # Both attempts consumed; sleep called once (only on attempt 0, not attempt 1)
    assert mock_bot.send_message.await_count == 2
    mock_sleep.assert_awaited_once_with(1)
    # Retry-exhausted path: logger.error "flood retry failed" (line 217-221),
    # NOT the attempt-0 logger.warning "flood control, retry after" (line 209-214).
    assert any(
        r.levelname == "ERROR" and "flood retry failed" in r.message for r in caplog.records
    ), "Expected ERROR log 'flood retry failed'"
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_send_reminder_generic_telegram_api_error(
    session_factory: Any, session: AsyncSession, scheduler: AsyncIOScheduler,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """send_reminder: generic TelegramAPIError (not Forbidden/BadRequest/RetryAfter)
    → log error, return. Covers scheduler.py:231-232, 238.

    Mirror of test_send_reminder_forbidden_logs_warning but with base
    TelegramAPIError (network error, 5xx). Single attempt, no retry.
    TelegramAPIError is the parent of Forbidden/BadRequest/RetryAfter, but
    `except (TelegramForbiddenError, TelegramBadRequest)` matches only subclass
    instances — a base-class instance falls through to `except TelegramAPIError`.

    caplog assertion distinguishes this branch (logger.error at line 232,
    "send_message failed") from the Forbidden/BadRequest branch (logger.warning
    at line 224, "chat issue"). Both produce await_count==1 — without caplog
    the test couldn't tell which except clause ran.
    """
    from aiogram.exceptions import TelegramAPIError

    _start_scheduler(scheduler)
    booking_start = datetime.now(UTC) - timedelta(hours=12)
    booking = await _seed_booking(session, booking_start)

    mock_bot = AsyncMock()
    method = MagicMock()
    mock_bot.send_message.side_effect = TelegramAPIError(
        method=method, message="internal server error"
    )

    with (
        patch("scheduler._bot_ref", mock_bot),
        patch("bot.db.async_session_factory", session_factory),
    ):
        await send_reminder(booking.id, "remind_24h", bot=mock_bot)

    # Single attempt, no retry (generic API error ≠ RetryAfter)
    assert mock_bot.send_message.await_count == 1
    # Generic API error path: logger.error "send_message failed" (line 232-236),
    # NOT the Forbidden/BadRequest logger.warning "chat issue" (line 224-229).
    assert any(
        r.levelname == "ERROR" and "send_message failed" in r.message for r in caplog.records
    ), "Expected ERROR log 'send_message failed'"
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_schedule_for_booking_skips_both_past_due(
    scheduler: AsyncIOScheduler,
) -> None:
    """Both remind_24h and remind_1h past-due → skip both silently.

    Covers scheduler.py:297 (remind_1h past-due skip log). The existing
    test_schedule_for_booking_skips_past_due_remind_24h covers the case
    where remind_24h is past-due but remind_1h is future (start_at = now+7h).
    This test covers the edge case where the booking is fully in the past
    (start_at = now-25h): both reminders are past-due → both skipped.

    Real scenario: bot offline >24h during a booking window. on_startup_scan
    reschedules and must skip ancient reminders (don't fire "завтра в 14:00"
    for a booking that was 2 days ago).
    """
    _start_scheduler(scheduler)

    booking_id = uuid4()
    start_at = datetime.now(UTC) - timedelta(hours=25)

    schedule_for_booking(scheduler, booking_id, start_at)

    assert scheduler.get_job(f"remind_24h_{booking_id}") is None
    assert scheduler.get_job(f"remind_1h_{booking_id}") is None

    scheduler.shutdown(wait=False)
