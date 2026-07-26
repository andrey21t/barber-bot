"""Inline keyboards for booking flow — date picker, slot picker, confirm.

AI calendar: using simple 7-day picker (next 7 days from today) instead of aiogram_calendar
in Шаге 3 — simpler to test, 0 navigation state. aiogram_calendar available in deps for
future month-navigation in Урок 2.5+.

CallbackData factories (aiogram 3.x):
- BookDateCallbackData: prefix="book_date", iso: str (ISO date)
- BookSlotCallbackData: prefix="book_slot", slot_id: UUID
- BookConfirmCallbackData: prefix="book_confirm"
- BookCancelCallbackData: prefix="book_cancel"

Note: prefix uses '_' not ':' — aiogram 3.x forbids separator ':' inside prefix
(ValueError: "Separator symbol ':' can not be used inside prefix").
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import Slot


class BookDateCallbackData(CallbackData, prefix="book_date"):
    """Date picker callback — payload is ISO date string."""

    iso: str


class BookSlotCallbackData(CallbackData, prefix="book_slot"):
    """Slot picker callback — payload is slot UUID."""

    slot_id: UUID


class BookConfirmCallbackData(CallbackData, prefix="book_confirm"):
    """Confirm booking callback — no payload."""


class BookCancelCallbackData(CallbackData, prefix="book_cancel"):
    """Cancel booking callback — no payload."""


def date_picker_keyboard(days_ahead: int = 7) -> InlineKeyboardMarkup:
    """Build inline keyboard with next N days.

    Each button shows date in human-readable format (e.g. "17 марта"),
    callback_data carries ISO date string.
    """
    builder = InlineKeyboardBuilder()
    today = datetime.now(UTC).date()
    month_names = [
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        cb = BookDateCallbackData(iso=d.isoformat())
        label = f"{d.day} {month_names[d.month]}"
        builder.button(text=label, callback_data=cb.pack())
    builder.adjust(2)  # 2 buttons per row
    return builder.as_markup()


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
    return (
        f"📅 {formatted}\n"
        f"💇 {service_title}\n"
        f"👤 {client_name}\n"
    )
