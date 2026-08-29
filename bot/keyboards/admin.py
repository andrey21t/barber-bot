"""Keyboards for master (admin) — inline menu + back-compat reply keyboard.

Spec.md 251 (Вариант B): inline keyboard с 5 кнопками для мастера Екатерины.
Каждая кнопка триггерит callback → FSM flow (multi-step для 3 из 5):
- ➕ Открыть слоты → adding_slots (date → hours)
- 🔒 Закрыть слот → closing_slot (date → hour)
- 📅 Сегодня → мгновенный список (no FSM)
- 🗓 Неделя → мгновенный список (no FSM)
- 💇 Добавить услугу → entering_service (name → duration → price)

Back-compat: admin_keyboard() (reply) оставлен как alias для тестов test_admin_handlers.py
(54 теста на command handlers) и для Екатерины если она запомнила команды.

Этап 5.9: admin_move keyboard + 3 callbacks (AdminMoveCallbackData,
AdminMoveSlot30CallbackData, AdminMoveConfirmCallbackData).
"""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_calendar import SimpleCalendar

from bot.models import Booking


class AdminMenuCallbackData(CallbackData, prefix="admin_menu"):
    """Empty callback — opens admin inline menu.

    Будет подключён в Этап 1.3 (handlers/start.py) для кнопки '📋 Меню'
    в welcome-сообщении мастера.
    """


class AdminOpendayCallbackData(CallbackData, prefix="admin_openday"):
    """Trigger opening_workday flow — открыть рабочий день (Этап 5.1).

    Replaces AdminAddslotsCallbackData as the primary 'open window' action —
    WorkDay [start_time, end_time] instead of per-hour Slot list. addslots
    button stays as deprecated alias until 5.10 (PLANS.md Plan of Work
    п.11), then removed from the inline menu.
    """


class AdminAddslotsCallbackData(CallbackData, prefix="admin_addslots"):
    """Trigger adding_slots flow — открыть слоты на дату (DEPRECATED after 5.1).

    Kept in the inline menu until 5.10 (PLANS.md Plan of Work п.11) for
    backwards compat with Екатерина's muscle memory; /addslots command alias
    stays even after the button is removed.
    """


class AdminCloseslotCallbackData(CallbackData, prefix="admin_closeslot"):
    """Trigger closing_slot flow — закрыть слот."""


class AdminTodayCallbackData(CallbackData, prefix="admin_today"):
    """Trigger today bookings list — мгновенный callback, no FSM."""


class AdminWeekCallbackData(CallbackData, prefix="admin_week"):
    """Trigger week bookings list — мгновенный callback, no FSM."""


class AdminServicesCallbackData(CallbackData, prefix="admin_services"):
    """Trigger entering_service flow — добавить услугу."""


class AdminMoveCallbackData(CallbackData, prefix="admin_move"):
    """Trigger admin_move flow — открыть calendar для переноса booking (Этап 5.9).

    Payload:
    - booking_id: UUID — booking to move (resolved from /today inline button).

    Distinct prefix from MyBookingsTransferCallbackData ("mybook_transfer") —
    that's client-initiated transfer with 24h rule + client_id pin. This is
    admin-initiated move without 24h rule, without client_id pin, with
    notification to CLIENT (not master). Different semantics, different prefix.
    """

    booking_id: UUID


class AdminMoveSlot30CallbackData(CallbackData, prefix="admin_move_slot_30"):
    """30-min WorkDay slot for admin_move flow (Этап 5.9).

    Mirror BookSlot30CallbackData (keyboards/client.py:80) but distinct prefix
    "admin_move_slot_30" — aiogram dispatch is exact-prefix match (callback_data.py:
    117-125), no conflict with "book_slot_30".

    Payload:
    - workday_id: UUID — WorkDay row (resolved by admin_move_simple_calendar_cb).
    - start_minute: int — minutes since midnight (0-1439), encodes start_time_local.
      int has no `:` → aiogram pack() safe (verified aiogram 3.x source).

    Conversion in admin_move_slot_30_cb: `dt_time(start_minute // 60, start_minute % 60)`.
    Wire format size: "admin_move_slot_30:<uuid>:<int>" ≈ 19+1+32+1+4 = 57 bytes < 64 limit.
    """

    workday_id: UUID
    start_minute: int


class AdminMoveConfirmCallbackData(CallbackData, prefix="admin_move_confirm"):
    """Confirm admin_move booking — final step in AdminMoveStates.confirming (Этап 5.9).

    No payload (mirror BookConfirmCallbackData pattern, keyboards/client.py:50).
    Handler reads booking_id + new_workday_id + new_start_minute from FSM state
    (stored in selecting_slot transition), NOT from callback payload — keeps
    callback_data small and avoids race where user could change FSM state mid-tap.
    """


def admin_inline_menu() -> InlineKeyboardMarkup:
    """Inline keyboard с 6 кнопками для мастера (spec.md 251, Вариант B + Этап 5.1).

    Layout: 3 + 2 + 1 (3 rows). Каждая кнопка = отдельный callback_data.
    Row 1 (открытие/закрытие): 📅 Открыть день (5.1, primary), ➕ Открыть слоты
        (deprecated alias до 5.10), 🔒 Закрыть слот (deprecated до 5.10).
    Row 2 (просмотр): 📅 Сегодня, 🗓 Неделя — мгновенные callbacks, no FSM.
    Row 3 (услуги): 💇 Добавить услугу — entering_service flow.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Открыть день", callback_data=AdminOpendayCallbackData().pack())
    builder.button(text="➕ Открыть слоты", callback_data=AdminAddslotsCallbackData().pack())
    builder.button(text="🔒 Закрыть слот", callback_data=AdminCloseslotCallbackData().pack())
    builder.button(text="📅 Сегодня", callback_data=AdminTodayCallbackData().pack())
    builder.button(text="🗓 Неделя", callback_data=AdminWeekCallbackData().pack())
    builder.button(text="💇 Добавить услугу", callback_data=AdminServicesCallbackData().pack())
    builder.adjust(3, 2, 1)
    return builder.as_markup()


async def admin_calendar_keyboard(min_date: datetime, max_date: datetime) -> InlineKeyboardMarkup:
    """SimpleCalendar для admin FSM (adding_slots_date / closing_slot_date).

    locale='ru_RU.UTF-8' — точное имя локали (как в `locale -a`). Без суффикса
    .UTF-8 setlocale падает на python:3.12-slim даже после locale-gen
    (incident Session 5.9 smoke test).
    Caller must `await` this function и strip tzinfo via .replace(tzinfo=None).
    """
    cal = SimpleCalendar(
        locale="ru_RU.UTF-8",
        cancel_btn="Отмена",
        today_btn="Сегодня",
    )
    cal.set_dates_range(min_date=min_date, max_date=max_date)
    # aiogram_calendar has no type stubs — cast to satisfy mypy.
    return cast(InlineKeyboardMarkup, await cal.start_calendar())


def admin_keyboard() -> ReplyKeyboardMarkup:
    """Back-compat reply keyboard with 5 master commands (alias для команд).

    После Этапа 1.3 + Этапа 3 (Session 5.9) НЕ показывается в /start
    (заменён на admin_inline_menu). Оставлен как alias для:
    - test_admin_handlers.py (54 теста на command handlers)
    - Екатерины если она запомнила команды /addslots /closeslot /today /week /services
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/addslots"), KeyboardButton(text="/closeslot")],
            [KeyboardButton(text="/today"), KeyboardButton(text="/week")],
            [KeyboardButton(text="/services add")],
        ],
        resize_keyboard=True,
        is_persistent=False,
    )


def admin_today_keyboard(
    bookings: list[Booking],
    business_timezone: str = "Europe/Moscow",
) -> InlineKeyboardMarkup:
    """Inline keyboard with [🔄 Перенести] button for each today booking (Этап 5.9).

    One button per booking, labeled with local time + service title (matches
    /today text line). admin taps → admin_move flow (calendar → 30-min slot
    picker → admin_move_booking service).

    adjust(1) — one button per row (avoid horizontal clutter; Екатерина sees a
    list, not a grid). Telegram inline keyboard limit 100 buttons/row × N rows
    — pet-project single-tenant (Екатерина < 10 bookings/day), no pagination
    needed. If > 30 bookings — would need pagination (defer until pain).

    NB: workday-only bookings (slot_id is None) AND legacy slot-based bookings
    BOTH get [🔄 Перенести] button — admin_move_booking handles both paths
    (slot_id → NULL for legacy, no slot release for workday-only source).
    """
    tz = ZoneInfo(business_timezone)
    builder = InlineKeyboardBuilder()
    for b in bookings:
        # b.start_at: naive on SQLite, aware UTC on Postgres. Inject tzinfo=UTC
        # (no-op on Postgres) before .astimezone — Python interprets naive as
        # system-local TZ otherwise.
        local_time = b.start_at.replace(tzinfo=UTC).astimezone(tz)
        when = local_time.strftime("%H:%M")
        # Strip newlines from already-escaped snapshots to preserve button label
        # layout (mirror _render_bookings:564 in admin.py).
        name = b.client_name_snapshot.replace("\n", " ")
        service = b.service_title_snapshot.replace("\n", " ")
        builder.button(
            text=f"🔄 {when} — {name}, {service}",
            callback_data=AdminMoveCallbackData(booking_id=b.id).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def admin_move_confirm_keyboard() -> InlineKeyboardMarkup:
    """Build [✅ Перенести] / [❌ Отмена] keyboard for AdminMoveStates.confirming (Этап 5.9).

    Mirror confirm_keyboard() in keyboards/client.py:186 but uses
    AdminMoveConfirmCallbackData (distinct prefix, no conflict with
    BookConfirmCallbackData "book_confirm").
    """
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Перенести",
        callback_data=AdminMoveConfirmCallbackData().pack(),
    )
    builder.button(text="❌ Отмена", callback_data="admin_move_cancel")
    builder.adjust(2)
    # Suppress unused import warning (InlineKeyboardButton kept for clarity
    # if someone wants to extend with custom rows later).
    _ = InlineKeyboardButton
    return builder.as_markup()
