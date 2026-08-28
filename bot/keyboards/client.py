"""Inline keyboards for booking flow — date picker, slot picker, confirm.

Date picker: aiogram_calendar.SimpleCalendar (month navigation, ru_RU locale,
Russian cancel/today labels) replaces the old 7-day button list. Range is
bounded by min_date/max_date (today..today+MAX_BOOKING_DAYS_AHEAD in business
timezone). Caller must strip tzinfo via .replace(tzinfo=None) — aiogram_calendar
compares with naive datetime(year, month, day) internally (common.py:56).

CallbackData factories (aiogram 3.x):
- BookSlotCallbackData: prefix="book_slot", slot_id: UUID
- BookConfirmCallbackData: prefix="book_confirm"
- BookCancelCallbackData: prefix="book_cancel"  (booking flow cancel — no payload)
- MyBookingsCancelCallbackData: prefix="mybook_cancel", booking_id: UUID  (cancel existing booking)
- MyBookingsTransferCallbackData: prefix="mybook_transfer", booking_id: UUID
  (transfer existing booking — re-uses SimpleCalendar picker in subsequent FSM steps)

Note: prefix uses '_' not ':' — aiogram 3.x forbids separator ':' inside prefix
(ValueError: "Separator symbol ':' can not be used inside prefix").
"""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_calendar import SimpleCalendar

from bot.models import Booking, Slot


class BookSlotCallbackData(CallbackData, prefix="book_slot"):
    """Slot picker callback — payload is slot UUID."""

    slot_id: UUID


class BookConfirmCallbackData(CallbackData, prefix="book_confirm"):
    """Confirm booking callback — no payload."""


class BookCancelCallbackData(CallbackData, prefix="book_cancel"):
    """Cancel booking FLOW callback (cancels FSM input, NOT a stored booking) — no payload."""


class MyBookingsCancelCallbackData(CallbackData, prefix="mybook_cancel"):
    """Cancel an existing booking via /mybookings inline button — payload is booking UUID.

    Distinct prefix from BookCancelCallbackData (which cancels the FSM input flow,
    not a persisted booking). StateFilter(None) — cancel works only outside FSM.
    """

    booking_id: UUID


class MyBookingsTransferCallbackData(CallbackData, prefix="mybook_transfer"):
    """Transfer an existing booking via /mybookings inline button — payload is booking UUID.

    Re-uses SimpleCalendar picker (SimpleCalendarCallback) in subsequent FSM steps,
    but the entry-point button uses this distinct prefix so handler can resolve
    booking_id and validate it's still cancelable (>24h). StateFilter(None) —
    entry works only outside FSM (consistent with mybook_cancel).
    """

    booking_id: UUID


async def calendar_keyboard(min_date: datetime, max_date: datetime) -> InlineKeyboardMarkup:
    """Build SimpleCalendar markup with date range.

    Args:
        min_date, max_date: NAIVE datetimes (no tzinfo) in business timezone.
        Caller must strip tzinfo via .replace(tzinfo=None) — aiogram_calendar
        compares with naive datetime(year, month, day) internally (common.py:56).

    Returns:
        InlineKeyboardMarkup with month grid + navigation (<<, <, >, >>) +
        Russian "Отмена" / "Сегодня" buttons.
    """
    cal = SimpleCalendar(
        locale="ru_RU.UTF-8",
        cancel_btn="Отмена",
        today_btn="Сегодня",
    )
    cal.set_dates_range(min_date=min_date, max_date=max_date)
    # aiogram_calendar has no type stubs — cast to satisfy mypy (lib returns InlineKeyboardMarkup).
    return cast(InlineKeyboardMarkup, await cal.start_calendar())


def slot_picker_keyboard(slots: list[Slot]) -> InlineKeyboardMarkup:
    """Build inline keyboard with available slots.

    Each button shows slot_hour (e.g. "14:00"), callback_data carries slot UUID.
    Empty slots list → single "Нет свободных слотов" button (disabled).
    """
    builder = InlineKeyboardBuilder()
    if not slots:
        builder.button(text="Нет свободных слотов", callback_data="noop")
        return builder.as_markup()

    for slot in slots:
        cb = BookSlotCallbackData(slot_id=slot.id)
        label = f"{slot.slot_hour:02d}:00"
        builder.button(text=label, callback_data=cb.pack())
    builder.adjust(3)  # 3 slots per row
    return builder.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Build ✅ Подтвердить / ❌ Отмена keyboard."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=BookConfirmCallbackData().pack())
    builder.button(text="❌ Отмена", callback_data=BookCancelCallbackData().pack())
    builder.adjust(2)
    return builder.as_markup()


def _no_op_button() -> InlineKeyboardButton:
    """Placeholder button for empty slots (used in tests)."""
    return InlineKeyboardButton(text="Нет свободных слотов", callback_data="noop")


def mybookings_keyboard(
    bookings: list[Booking],
    business_timezone: str = "Europe/Moscow",
) -> InlineKeyboardMarkup:
    """Build [❌ Отменить] + [🔄 Перенести] inline buttons for client's cancelable bookings.

    Two buttons per booking in one row (adjust(2)) — pairs [Отменить <date>] with
    [Перенести <date>] so user can pick either action. Each button labeled with
    local date+time (matches the line in /mybookings text list).

    Caller responsibility: filter bookings where `start_at - CANCEL_MIN_HOURS > now`
    (handler computes deadline, only passes cancelable bookings here). Both cancel
    and transfer share the same 24h window (spec.md 41 — "отмена (>24ч) или перенос
    (>24ч)"), so one cancelable list drives both buttons.
    """
    tz = ZoneInfo(business_timezone)
    builder = InlineKeyboardBuilder()
    for b in bookings:
        # b.start_at: naive on SQLite, aware UTC on Postgres. Inject tzinfo=UTC
        # (no-op on Postgres) before .astimezone — Python interprets naive as
        # system-local TZ otherwise.
        local_time = b.start_at.replace(tzinfo=UTC).astimezone(tz)
        when = local_time.strftime("%d %b %H:%M")
        builder.button(
            text=f"❌ Отменить {when}",
            callback_data=MyBookingsCancelCallbackData(booking_id=b.id).pack(),
        )
        builder.button(
            text=f"🔄 Перенести {when}",
            callback_data=MyBookingsTransferCallbackData(booking_id=b.id).pack(),
        )
    builder.adjust(2)  # 2 buttons per row: [Отменить] [Перенести] for each booking
    return builder.as_markup()


def _format_booking_summary(
    slot: Slot,
    client_name: str,
    service_title: str,
    business_timezone: str = "Europe/Moscow",
) -> str:
    """Format booking summary message for confirming state.

    Used by handler to render summary before ✅/❌ buttons.
    """
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    local_dt = datetime.combine(
        slot.slot_date, dtime(hour=slot.slot_hour), tzinfo=ZoneInfo(business_timezone)
    )
    formatted = local_dt.strftime("%d %B %Y, %H:%M")
    return f"📅 {formatted}\n💇 {service_title}\n👤 {client_name}\n"
