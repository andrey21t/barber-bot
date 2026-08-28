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
"""

from datetime import datetime
from typing import cast

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_calendar import SimpleCalendar


class AdminMenuCallbackData(CallbackData, prefix="admin_menu"):
    """Empty callback — opens admin inline menu.

    Будет подключён в Этап 1.3 (handlers/start.py) для кнопки '📋 Меню'
    в welcome-сообщении мастера.
    """


class AdminAddslotsCallbackData(CallbackData, prefix="admin_addslots"):
    """Trigger adding_slots flow — открыть слоты на дату."""


class AdminCloseslotCallbackData(CallbackData, prefix="admin_closeslot"):
    """Trigger closing_slot flow — закрыть слот."""


class AdminTodayCallbackData(CallbackData, prefix="admin_today"):
    """Trigger today bookings list — мгновенный callback, no FSM."""


class AdminWeekCallbackData(CallbackData, prefix="admin_week"):
    """Trigger week bookings list — мгновенный callback, no FSM."""


class AdminServicesCallbackData(CallbackData, prefix="admin_services"):
    """Trigger entering_service flow — добавить услугу."""


def admin_inline_menu() -> InlineKeyboardMarkup:
    """Inline keyboard с 5 кнопками для мастера (spec.md 251, Вариант B).

    Layout: 2 + 2 + 1 (3 rows). Каждая кнопка = отдельный callback_data.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Открыть слоты", callback_data=AdminAddslotsCallbackData().pack())
    builder.button(text="🔒 Закрыть слот", callback_data=AdminCloseslotCallbackData().pack())
    builder.button(text="📅 Сегодня", callback_data=AdminTodayCallbackData().pack())
    builder.button(text="🗓 Неделя", callback_data=AdminWeekCallbackData().pack())
    builder.button(text="💇 Добавить услугу", callback_data=AdminServicesCallbackData().pack())
    builder.adjust(2, 2, 1)
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
