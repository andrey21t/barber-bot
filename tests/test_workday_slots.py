"""Tests for WorkDay-based 30-min slot helpers — Этап 5.4 (PLANS.md Gap 1, Blocker C).

Coverage:
- _build_start_at_from_workday: WorkDay.work_date + LOCAL time → UTC datetime
- get_30min_slots_from_workday: 30-min grid [start_time, end_time), past-filter
- slot_picker_keyboard_30min: keyboard layout (3 per row, empty-state)

NOT covered: occupancy filter (5.6 «Мест нет»), /slots handler integration (5.8).
These helpers are infrastructure released in 5.4 — the actual /book flow still
uses legacy slot-based path (BookingCreate.slot_id → _build_start_at).
"""

from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

import pytest
from aiogram.types import InlineKeyboardMarkup
from bot.keyboards.client import slot_picker_keyboard_30min
from bot.models import WorkDay
from bot.services.booking import _build_start_at_from_workday
from bot.services.slots import (
    TimeSlot30,
    get_30min_slots_from_workday,
)


# ============================================================
# Helpers
# ============================================================
def _make_workday(
    *,
    work_date: date,
    start_time: time,
    end_time: time,
    master_id: UUID | None = None,
) -> WorkDay:
    """Build a WorkDay instance in-memory (no session — helpers are pure)."""
    return WorkDay(
        master_id=master_id or uuid4(),
        work_date=work_date,
        start_time=start_time,
        end_time=end_time,
        max_concurrent_clients=1,
        is_active=True,
    )


# ============================================================
# _build_start_at_from_workday
# ============================================================
@pytest.mark.asyncio
async def test_build_start_at_from_workday_30min() -> None:
    """WorkDay.work_date + LOCAL time HH:MM → aware UTC datetime (30-min granularity).

    Moscow (UTC+3): 2026-03-15 14:30 LOCAL → 2026-03-15 11:30 UTC.
    Covers the 30-min case that slot.slot_hour (int 0-23) cannot represent.
    """
    workday = _make_workday(
        work_date=datetime.fromisoformat("2026-03-15").date(),
        start_time=time(10, 0),
        end_time=time(20, 0),
    )
    start_at = _build_start_at_from_workday(workday, time(14, 30), "Europe/Moscow")
    expected = datetime(2026, 3, 15, 11, 30, tzinfo=UTC)
    assert start_at == expected
    assert start_at.tzinfo == UTC  # aware, not naive


# ============================================================
# get_30min_slots_from_workday
# ============================================================
@pytest.mark.asyncio
async def test_get_30min_slots_from_workday() -> None:
    """30-min grid from [10:00, 12:00) → 4 slots: 10:00, 10:30, 11:00, 11:30.

    Half-open [start, end): a slot starting exactly at end_time (12:00) is
    excluded because its end_at (12:30) would exceed end_time — violates
    WorkDay invariant (5.3). With 30-min step, the last slot starts at
    end_time - 30min = 11:30.
    """
    workday = _make_workday(
        work_date=datetime.fromisoformat("2026-03-15").date(),
        start_time=time(10, 0),
        end_time=time(12, 0),
    )
    # now_utc far in past — no past-filter applies (full grid returned).
    now_utc = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    slots = await get_30min_slots_from_workday(workday, "Europe/Moscow", now_utc=now_utc)

    assert len(slots) == 4
    # Labels: HH:MM in business tz (pre-formatted by helper).
    assert [s.label for s in slots] == ["10:00", "10:30", "11:00", "11:30"]
    # start_at_utc ordered ascending, all in window [10:00, 12:00) LOCAL.
    expected_utc = [
        datetime(2026, 3, 15, 7, 0, tzinfo=UTC),  # 10:00 LOCAL = 07:00 UTC
        datetime(2026, 3, 15, 7, 30, tzinfo=UTC),  # 10:30 LOCAL = 07:30 UTC
        datetime(2026, 3, 15, 8, 0, tzinfo=UTC),  # 11:00 LOCAL = 08:00 UTC
        datetime(2026, 3, 15, 8, 30, tzinfo=UTC),  # 11:30 LOCAL = 08:30 UTC
    ]
    assert [s.start_at_utc for s in slots] == expected_utc
    # start_time_local matches start_at_utc converted back to LOCAL.
    assert [s.start_time_local for s in slots] == [
        time(10, 0),
        time(10, 30),
        time(11, 0),
        time(11, 30),
    ]


@pytest.mark.asyncio
async def test_get_30min_slots_filters_past() -> None:
    """now_utc within the window → only slots with start_at_utc > now_utc returned.

    WorkDay [10:00, 12:00) LOCAL Moscow, now_utc = 11:00 LOCAL (08:00 UTC).
    Expected: 11:00 (start_at > now_utc? — strictly greater, equal excluded
    since booking exactly at now is effectively in the past) — wait, check the
    helper contract: 'if start_at_utc > ref: keep'. So 11:00 is KEPT (08:00 UTC
    > 08:00 UTC is False — equal excluded). Re-read helper: `>`, not `>=`.

    Re-derive: now_utc = 08:00 UTC (= 11:00 LOCAL). Slot 11:00 LOCAL →
    start_at_utc = 08:00 UTC. `if start_at_utc > ref` → 08:00 > 08:00 = False
    → excluded. So only 11:30 kept. Let's verify that's intentional: at 11:00:00
    sharp the slot window is already in the past (you can't book starting NOW
    at the exact minute — needs at least a few minutes of lead time in
    practice). The helper uses strict `>` for simplicity; refine later if UX
    requires a "now + N minutes" buffer.

    Final expectation: only 11:30 slot remains.
    """
    workday = _make_workday(
        work_date=datetime.fromisoformat("2026-03-15").date(),
        start_time=time(10, 0),
        end_time=time(12, 0),
    )
    # 11:00 LOCAL = 08:00 UTC — exactly at the boundary of the slot start.
    now_utc = datetime(2026, 3, 15, 8, 0, tzinfo=UTC)
    slots = await get_30min_slots_from_workday(workday, "Europe/Moscow", now_utc=now_utc)

    # Only 11:30 (start_at_utc 08:30 > now_utc 08:00).
    assert len(slots) == 1
    assert slots[0].label == "11:30"
    assert slots[0].start_at_utc == datetime(2026, 3, 15, 8, 30, tzinfo=UTC)


# ============================================================
# slot_picker_keyboard_30min
# ============================================================
def test_slot_picker_30min_keyboard() -> None:
    """Keyboard layout — 3 buttons per row, HH:MM labels, empty-state fallback.

    Verifies:
    - Non-empty input → adjust(3), buttons with pre-formatted labels.
    - Empty input → single "Нет свободных слотов" button.
    - callback_data placeholder "noop" (TODO 5.8: BookSlot30CallbackData).
    """
    # Non-empty: 4 slots → 4 buttons, adjust(3) → 2 rows (3 + 1).
    slots = [
        TimeSlot30(
            start_at_utc=datetime(2026, 3, 15, 7, 0, tzinfo=UTC),
            start_time_local=time(10, 0),
            label="10:00",
        ),
        TimeSlot30(
            start_at_utc=datetime(2026, 3, 15, 7, 30, tzinfo=UTC),
            start_time_local=time(10, 30),
            label="10:30",
        ),
        TimeSlot30(
            start_at_utc=datetime(2026, 3, 15, 8, 0, tzinfo=UTC),
            start_time_local=time(11, 0),
            label="11:00",
        ),
        TimeSlot30(
            start_at_utc=datetime(2026, 3, 15, 8, 30, tzinfo=UTC),
            start_time_local=time(11, 30),
            label="11:30",
        ),
    ]
    kb = slot_picker_keyboard_30min(slots)
    assert isinstance(kb, InlineKeyboardMarkup)
    # aiogram InlineKeyboardMarkup exposes .inline_keyboard as list of rows.
    rows = kb.inline_keyboard
    # adjust(3) groups buttons into rows of 3 then 1: row0=3 btns, row1=1 btn.
    assert len(rows) == 2
    assert len(rows[0]) == 3
    assert len(rows[1]) == 1
    # Labels preserved in order.
    labels_row0 = [btn.text for btn in rows[0]]
    labels_row1 = [btn.text for btn in rows[1]]
    assert labels_row0 == ["10:00", "10:30", "11:00"]
    assert labels_row1 == ["11:30"]
    # callback_data placeholder (TODO 5.8: real BookSlot30CallbackData).
    assert rows[0][0].callback_data == "noop"

    # Empty input → single "Нет свободных слотов" button.
    kb_empty = slot_picker_keyboard_30min([])
    rows_empty = kb_empty.inline_keyboard
    assert len(rows_empty) == 1
    assert len(rows_empty[0]) == 1
    assert rows_empty[0][0].text == "Нет свободных слотов"
