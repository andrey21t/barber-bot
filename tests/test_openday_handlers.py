"""Tests for bot.handlers.admin cmd_openday — F1 UX fix verification (Session 5.18).

Coverage (NEXT_SESSION_PROMPT.md 5.18):
- test_openday_command_text_args_creates_workday: happy path — /openday <date> 11:00 18:00
  → '✅ День открыт' + WorkDay row with start=11:00, end=18:00, is_active=True.
- test_openday_idempotent_update_no_duplicate_row: existing [11:00, 18:00] → /openday
  [10:00, 19:00] on same date → UPDATE (no INSERT), no duplicate, no re-open message.
- test_openday_shrink_error_message_rendered: existing [10:00, 20:00] + active booking
  [19:00, 20:00] → /openday [10:00, 18:00] (shrink end) → WorkDayShrinkError → message
  'Нельзя сократить окно — есть активные записи'.
- test_openday_reopen_shows_message: existing is_active=False (closed day) → /openday →
  re-open. F1 regression — message MUST contain 'день был закрыт — открыт заново'
  (was_closed=True). Before F1 fix (variant B), handler checked workday.is_active
  AFTER open_workday refresh (always True) → message never shown.
- test_openday_open_no_reopen_message_when_was_active: existing is_active=True → /openday
  (idempotent) → message must NOT contain 'день был закрыт' (was_closed=False).

Pattern: mirror tests/test_admin_handlers.py — direct handler invocation with mock
Message + CommandObject + monkeypatch of `bot.handlers.admin.async_session_factory`
so handler DB calls hit in-memory SQLite. Avoids `dp.feed_update` ceremony.

Why handler tests (not just service tests):
- cmd_openday has UX partition (was_closed message) computed in the handler — F1 bug
  lived in handler-level `is_active` check (admin.py:332, 1060). Service tests on
  open_workday pass (is_active=True after re-open is correct contract), but the UX
  message never triggered. Handler tests catch the regression.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from bot.handlers import admin as admin_handlers
from bot.models import WorkDay
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_admin_handlers import (
    ADMIN_TG_ID,
    TZ,
    _answer_text,
    _local_to_utc_naive,
    _make_command,
    _make_message,
    _seed_admin_stack,
    _seed_booking,
    _seed_slot,
)

# ============================================================
# Fixture — patch async_session_factory in admin handlers module
# (mirror of test_admin_handlers.py:patched_session_factory)
# ============================================================


@pytest.fixture
def patched_session_factory(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Replace `bot.handlers.admin.async_session_factory` with the test engine's
    session factory so handler DB calls hit in-memory SQLite.
    """
    monkeypatch.setattr(admin_handlers, "async_session_factory", session_factory)
    return session_factory


# ============================================================
# Helper — seed WorkDay directly (bypass service for pre-existing state)
# ============================================================


async def _seed_workday(
    session: AsyncSession,
    *,
    master_id: UUID,
    work_date: datetime,
    start_time: dt_time,
    end_time: dt_time,
    is_active: bool = True,
) -> WorkDay:
    """Insert a WorkDay row directly. Used to set up pre-existing state
    (closed day for F1 re-open test, opened day for idempotent test).
    """
    wd = WorkDay(
        master_id=master_id,
        work_date=work_date,
        start_time=start_time,
        end_time=end_time,
        max_concurrent_clients=1,
        is_active=is_active,
    )
    session.add(wd)
    await session.commit()
    return wd


def _tomorrow() -> datetime:
    """Future date — guaranteed > today_local (avoids past-date rejection)."""
    return (datetime.now(UTC) + timedelta(days=1)).date()


def _future(days: int) -> datetime:
    """Future date N days ahead (used by shrink test to avoid same-day overlaps)."""
    return (datetime.now(UTC) + timedelta(days=days)).date()


# ============================================================
# Tests — 5 cmd_openday handler tests (F1 UX fix verification)
# ============================================================


@pytest.mark.asyncio
async def test_openday_command_text_args_creates_workday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy: '/openday <future_date> 11:00 18:00' → '✅ День открыт' + WorkDay row
    with start_time=11:00, end_time=18:00, is_active=True.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    target = _tomorrow()
    msg = _make_message(
        user_id=ADMIN_TG_ID,
        text=f"/openday {target} 11:00 18:00",
    )
    await admin_handlers.cmd_openday(msg, _make_command(f"{target} 11:00 18:00"))

    text = _answer_text(msg)
    assert "✅ День открыт" in text
    assert "11:00" in text and "18:00" in text

    async with session_factory() as verify:
        workdays = (await verify.execute(select(WorkDay))).scalars().all()
        assert len(workdays) == 1
        wd = workdays[0]
        assert wd.start_time == dt_time(11, 0)
        assert wd.end_time == dt_time(18, 0)
        assert wd.is_active is True


@pytest.mark.asyncio
async def test_openday_idempotent_update_no_duplicate_row(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Idempotent: existing WorkDay [11:00, 18:00] (is_active=True) → /openday
    [10:00, 19:00] on same date → UPDATE (not INSERT), no duplicate row,
    no 'день был закрыт' message (was_closed=False).
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        target = _tomorrow()
        await _seed_workday(
            session,
            master_id=ctx["master_id"],
            work_date=target,
            start_time=dt_time(11, 0),
            end_time=dt_time(18, 0),
            is_active=True,
        )

    target = _tomorrow()
    msg = _make_message(
        user_id=ADMIN_TG_ID,
        text=f"/openday {target} 10:00 19:00",
    )
    await admin_handlers.cmd_openday(msg, _make_command(f"{target} 10:00 19:00"))

    text = _answer_text(msg)
    assert "✅ День открыт" in text
    assert "10:00" in text and "19:00" in text
    assert "день был закрыт" not in text  # was_closed=False — no re-open

    async with session_factory() as verify:
        workdays = (await verify.execute(select(WorkDay))).scalars().all()
        assert len(workdays) == 1  # UPDATE, not INSERT
        wd = workdays[0]
        assert wd.start_time == dt_time(10, 0)  # window updated
        assert wd.end_time == dt_time(19, 0)
        assert wd.is_active is True


@pytest.mark.asyncio
async def test_openday_shrink_error_message_rendered(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """WorkDayShrinkError path: existing [10:00, 20:00] + active booking [19:00, 20:00]
    → /openday [10:00, 18:00] (shrink end) → 'Нельзя сократить окно — есть активные
    записи'.

    Mirror of test_workday_service.py:test_update_workday_shrink_with_active_bookings_refuse
    but invoked via cmd_openday handler (not service directly) — verifies handler UX
    catches the service exception and renders the message.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        target = _future(11)
        await _seed_workday(
            session,
            master_id=ctx["master_id"],
            work_date=target,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            is_active=True,
        )
        # Booking [19:00, 20:00] LOCAL — inside the original window
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=target,
            hour=19,
            status="open",
        )
        booking_local_at_19 = datetime.combine(target, dt_time(19, 0), tzinfo=ZoneInfo(TZ))
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(booking_local_at_19),
            status="confirmed",
        )

    target = _future(11)
    msg = _make_message(
        user_id=ADMIN_TG_ID,
        text=f"/openday {target} 10:00 18:00",  # shrink end 20:00 → 18:00
    )
    await admin_handlers.cmd_openday(msg, _make_command(f"{target} 10:00 18:00"))

    text = _answer_text(msg)
    assert "Нельзя сократить окно" in text
    assert "активные записи" in text
    assert "/cancelbooking" in text

    async with session_factory() as verify:
        workdays = (await verify.execute(select(WorkDay))).scalars().all()
        assert len(workdays) == 1
        # Window NOT shrunk (handler rejected via service exception)
        assert workdays[0].end_time == dt_time(20, 0)


@pytest.mark.asyncio
async def test_openday_reopen_shows_message(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """F1 regression: existing WorkDay is_active=False (closed) → /openday → re-open.
    Handler MUST show 'день был закрыт — открыт заново' (was_closed=True).

    Before F1 fix (variant B): handler checked `workday.is_active` AFTER open_workday
    refresh — refresh returns is_active=True (re-open branch restores it) →
    `"" if True else "..."` = `""` → message NEVER shown. F1 fixed by capturing
    `was_closed` BEFORE open_workday in handler.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        target = _tomorrow()
        await _seed_workday(
            session,
            master_id=ctx["master_id"],
            work_date=target,
            start_time=dt_time(11, 0),
            end_time=dt_time(18, 0),
            is_active=False,  # ← closed day — re-open scenario
        )

    target = _tomorrow()
    msg = _make_message(
        user_id=ADMIN_TG_ID,
        text=f"/openday {target} 10:00 19:00",  # new window
    )
    await admin_handlers.cmd_openday(msg, _make_command(f"{target} 10:00 19:00"))

    text = _answer_text(msg)
    assert "✅ День открыт" in text
    # F1 fix verification — re-open notice MUST show (was_closed=True)
    assert "день был закрыт — открыт заново" in text

    async with session_factory() as verify:
        workdays = (await verify.execute(select(WorkDay))).scalars().all()
        assert len(workdays) == 1
        wd = workdays[0]
        assert wd.is_active is True  # re-opened
        assert wd.start_time == dt_time(10, 0)  # window updated
        assert wd.end_time == dt_time(19, 0)


@pytest.mark.asyncio
async def test_openday_open_no_reopen_message_when_was_active(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """F1 regression complement: existing is_active=True (already open) → /openday
    (idempotent UPDATE) → message must NOT contain 'день был закрыт'
    (was_closed=False — day was already active, no re-open happened).
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        target = _tomorrow()
        await _seed_workday(
            session,
            master_id=ctx["master_id"],
            work_date=target,
            start_time=dt_time(11, 0),
            end_time=dt_time(18, 0),
            is_active=True,  # already active — no re-open
        )

    target = _tomorrow()
    msg = _make_message(
        user_id=ADMIN_TG_ID,
        text=f"/openday {target} 11:00 18:00",  # same window → idempotent
    )
    await admin_handlers.cmd_openday(msg, _make_command(f"{target} 11:00 18:00"))

    text = _answer_text(msg)
    assert "✅ День открыт" in text
    # F1 fix verification — no re-open notice (was_closed=False)
    assert "день был закрыт" not in text

    async with session_factory() as verify:
        workdays = (await verify.execute(select(WorkDay))).scalars().all()
        assert len(workdays) == 1  # UPDATE, not INSERT
        assert workdays[0].is_active is True
