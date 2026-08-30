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

from bot.models import Booking, Service, Slot
from bot.services.slots import TimeSlot30


class BookSlotCallbackData(CallbackData, prefix="book_slot"):
    """Slot picker callback — payload is slot UUID.

    NB: used by legacy slot-based /book flow (slot_picker_keyboard). The new
    30-мин WorkDay-based flow (slot_picker_keyboard_30min, Этап 5.4) does NOT
    have a slot UUID — booking is created from WorkDay + selected start_time.
    /slots UI in 5.8 will introduce a new CallbackData carrying workday_id +
    start_time_local (or a synthetic key) — until then this prefix is shared
    by both flows (kept simple for 5.4 helper release; 5.8 may introduce a
    distinct prefix if the payload differs significantly).
    """

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


class BookSlot30CallbackData(CallbackData, prefix="book_slot_30"):
    """30-min WorkDay slot callback (Этап 5.8b — /slots UI workday path).

    Payload:
    - workday_id: UUID — WorkDay row in DB (resolved by simple_calendar_cb slots branch).
    - start_minute: int — minutes since midnight (0-1439), encodes start_time_local.
      Stored as int (NOT "HH:MM" str) — aiogram CallbackData.pack() raises ValueError
      if separator ':' appears inside a value (verified aiogram 3.x source:
      filters/callback_data.py:93-98, `__separator__ = ":"` default). int has no
      `:` → pack() safe. Range check in slot_30_cb handler (defensive, 0 <= x <= 1439).

    Conversion in slot_30_cb: `dt_time(start_minute // 60, start_minute % 60)`.
    Wire format size: "book_slot_30:<uuid>:<int>" ≈ 12+1+32+1+4 = 50 bytes < 64 limit.

    Distinct prefix from BookSlotCallbackData ("book_slot") — aiogram dispatch is
    exact-prefix match (callback_data.py:117-125), no substring conflict.
    """

    workday_id: UUID
    start_minute: int


class BookServiceCallbackData(CallbackData, prefix="book_service"):
    """Service picker callback (Session 5.27 FEAT — tap-to-select services).

    Payload: service_id UUID — Service row selected from inline picker.
    Handler resolves service.name + service.duration_minutes for summary +
    create_booking (BookingCreate.service_id set → _build_end_at uses
    service.duration_minutes, not SERVICE_DEFAULT_DURATION_MIN).

    Distinct prefix "book_service" — no conflict with booking flow callbacks
    (BookSlotCallbackData prefix="book_slot", BookSlot30CallbackData prefix="book_slot_30").

    Fallback "Своя услуга" uses plain string "book_service_custom" (no payload)
    caught by F.data == "book_service_custom" filter — handler keeps FSM in
    entering_service and asks for text; existing service_msg catches the
    free-text answer (legacy path, no service_id set → default duration).
    """

    service_id: UUID


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

    DEPRECATED (Этап 5.4): kept for the legacy slot-based /book flow until
    5.8 introduces /slots command + WorkDay-based booking. New code should
    use `slot_picker_keyboard_30min` which renders TimeSlot30 buttons with
    "HH:MM" labels (30-min step grid from WorkDay).

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


def slot_picker_keyboard_30min(
    slots: list[TimeSlot30],
    workday_id: UUID,
) -> InlineKeyboardMarkup:
    """Build inline keyboard from 30-мин WorkDay slots (Этап 5.4 → 5.8b wired).

    Each TimeSlot30 carries a pre-formatted `label` ("HH:MM" in business tz),
    so this helper does not need to know the timezone — generation logic lives
    in `get_30min_slots_from_workday` (separation of concerns: grid generation
    vs keyboard layout).

    Empty list → single "Нет свободных слотов" button (matches legacy
    slot_picker_keyboard UX). Adjust(3) — 3 buttons per row.

    Этап 5.8b (was: "noop" placeholder in 5.4 — 5.8b wires up real callback):
    callback_data carries BookSlot30CallbackData(workday_id, start_minute).
    `start_minute` = `slot.start_time_local.hour * 60 + slot.start_time_local.minute`
    (int 0-1439, no `:` — aiogram pack() safe per aiogram 3.x source).

    Caller MUST pass `workday_id` (resolved in simple_calendar_cb slots branch
    via `_select_workday_for_slot` or equivalent — single source of truth).
    """
    builder = InlineKeyboardBuilder()
    if not slots:
        builder.button(text="Нет свободных слотов", callback_data="noop")
        return builder.as_markup()

    for slot in slots:
        start_minute = slot.start_time_local.hour * 60 + slot.start_time_local.minute
        cb = BookSlot30CallbackData(
            workday_id=workday_id,
            start_minute=start_minute,
        )
        builder.button(text=slot.label, callback_data=cb.pack())
    builder.adjust(3)
    return builder.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Build ✅ Подтвердить / ❌ Отмена keyboard."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=BookConfirmCallbackData().pack())
    builder.button(text="❌ Отмена", callback_data=BookCancelCallbackData().pack())
    builder.adjust(2)
    return builder.as_markup()


def service_picker_keyboard(services: list[Service]) -> InlineKeyboardMarkup:
    """Build inline keyboard with services for tap-to-select (Session 5.27 FEAT).

    Each button shows service.name (up to 255 chars — Telegram truncates display
    if too long). callback_data carries BookServiceCallbackData(service_id).
    Last row adds "✏️ Своя услуга" (callback_data="book_service_custom") as a
    fallback to the legacy free-text input — covers unusual requests that
    don't match the predefined service list.

    Empty services list is handled by the caller (name_msg shows text prompt
    instead when no services in DB — single-master MVP, rare case).

    Args:
        services: list of bot.models.Service (active, business-scoped).

    Returns:
        InlineKeyboardMarkup — buttons in 2 columns, custom row last.
    """
    builder = InlineKeyboardBuilder()
    for svc in services:
        cb = BookServiceCallbackData(service_id=svc.id)
        builder.button(text=svc.name, callback_data=cb.pack())
    builder.button(text="✏️ Своя услуга", callback_data="book_service_custom")
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

    Этап 5.8b — Gap 5 fix (PLANS.md:263, :245): workday-only bookings
    (b.slot_id is None, created via /slots workday path) НЕ показывают [🔄 Перенести]
    кнопку — `transfer_booking` raises NotImplementedError (booking.py:920, 5.9 scope
    admin_move_booking). Hide-transfer prevents silent failure: user видит только
    [Отменить], не получает unhandled NotImplementedError при тапе.
    adjust(2) → uneven rows для workday-only (1 кнопка в ряду) — aiogram handles.
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
        # Gap 5 fix: skip [🔄 Перенести] для workday-only bookings (slot_id is None).
        # transfer_booking raises NotImplementedError (booking.py:920, 5.9 scope).
        if b.slot_id is not None:
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

    DEPRECATED (Этап 5.4): kept for the legacy slot-based /book flow. New code
    should use `_format_booking_summary_from_start_at` which takes the
    Booking.start_at (UTC datetime) directly — works for both slot-based and
    WorkDay-based bookings, decouples summary rendering from the Slot model.

    Used by handler to render summary before ✅/❌ buttons.
    """
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    local_dt = datetime.combine(
        slot.slot_date, dtime(hour=slot.slot_hour), tzinfo=ZoneInfo(business_timezone)
    )
    formatted = local_dt.strftime("%d %B %Y, %H:%M")
    return f"📅 {formatted}\n💇 {service_title}\n👤 {client_name}\n"


def _format_booking_summary_from_start_at(
    start_at: datetime,
    client_name: str,
    service_title: str,
    business_timezone: str = "Europe/Moscow",
) -> str:
    """Format booking summary from Booking.start_at (UTC) — Этап 5.4.

    Decouples summary rendering from the Slot model: works for slot-based
    bookings (legacy /addslots) AND WorkDay-based bookings (5.8 /slots).
    start_at is aware UTC (built by _build_start_at or _build_start_at_from_workday).

    Defensive .replace(tzinfo=UTC): converts DB-read naive datetime (SQLite stores
    naive) to aware UTC. NB: no-op ONLY when start_at is already aware UTC OR
    naive — if a caller passes aware non-UTC (e.g. Moscow tzinfo), .replace would
    overwrite tzinfo without conversion (wall-clock interpreted as UTC → silent
    3h shift for Moscow). Contract: caller passes aware UTC (in-memory built) or
    naive (DB-read); aware non-UTC is a contract violation.

    Args:
        start_at: aware UTC datetime OR naive (treated as UTC) — Booking.start_at.
        client_name, service_title: caller responsibility to html.escape()
            (helper renders as-is in HTML parse mode).
        business_timezone: IANA tz name for LOCAL rendering.
    """
    from zoneinfo import ZoneInfo

    local_time = start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_timezone))
    formatted = local_time.strftime("%d %B %Y, %H:%M")
    return f"📅 {formatted}\n💇 {service_title}\n👤 {client_name}\n"
