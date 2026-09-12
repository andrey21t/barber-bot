"""Keyboards for master (admin) — inline menu + back-compat reply keyboard.

Session 5.63 (пункт 3): inline keyboard с 5 кнопками для мастера Екатерины
(«Открыть день» удалён в 5.62 пункт 2, «Закрыть день» удалён в 5.63 пункт 3).
CREATE day через «Открыть неделю» или /openday текст. CLOSE day через «Сегодня»
view (admin_today_keyboard добавляет [🔒 Закрыть день] если есть активный
WorkDay) или /closeday текст (power-user shortcut).
Каждая кнопка триггерит callback → FSM flow (multi-step для 3 из 5):
- ➕ Изменить окно → adding_slots (date → start → end — MODIFY flow)
- 📅 Сегодня → мгновенный список (no FSM) + [🔒 Закрыть день] если WorkDay active
- 🗓 Неделя → мгновенный список (no FSM)
- 🗓 Открыть неделю → opening_week (batch CREATE, 5.26)
- 💇 Услуги → entering_service (name → duration → price)

/closeslot SHRINK inline flow REMOVED (5.10 simplification) — «Изменить окно»
умеет и расширить, и сузить, и сдвинуть (двухфазный picker start→end).

Back-compat: admin_keyboard() (reply) оставлен как alias для тестов test_admin_handlers.py
(54 теста на command handlers) и для Екатерины если она запомнила команды.

Этап 5.9: admin_move keyboard + 3 callbacks (AdminMoveCallbackData,
AdminMoveSlot30CallbackData, AdminMoveConfirmCallbackData).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as dt_time
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_calendar import SimpleCalendar

from bot.models import Booking, WorkDay


class AdminMenuCallbackData(CallbackData, prefix="admin_menu"):
    """Empty callback — opens admin inline menu.

    Будет подключён в Этап 1.3 (handlers/start.py) для кнопки '📋 Меню'
    в welcome-сообщении мастера.
    """


class AdminAddslotsCallbackData(CallbackData, prefix="admin_addslots"):
    """Trigger adding_slots flow — «Изменить окно» (MODIFY) — Этап 5.10.

    /closeslot SHRINK inline flow REMOVED (5.10 simplification) — «Изменить
    окно» handles both shrink+extend+shift via two-phase picker start→end.
    /addslots command alias stays for muscle memory (cmd_addslots redirect
    to calendar → inline picker).
    """


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
    """Inline keyboard с 5 кнопками для мастера (5.63).

    Session 5.62 (пункт 2 от Екатерины): кнопка «Открыть день» (старый
    текстовый формат с HH:MM input) УДАЛЕНА. CREATE day теперь только через
    «🗓 Открыть неделю» (inline picker) или текстовую команду /openday
    (power-user shortcut, без inline UI). MODIFY остаётся «➕ Изменить окно».

    Session 5.63 (пункт 3 от Екатерины): кнопка «Закрыть день» УДАЛЕНА из
    inline menu. CLOSE day теперь через «📅 Сегодня» view (admin_today_keyboard
    добавляет [🔒 Закрыть день] если есть активный WorkDay) или текстовую
    команду /closeday (power-user shortcut, без inline UI).

    Layout: 2 + 2 + 1 (3 rows).
    Row 1: ➕ Изменить окно (MODIFY, 5.10), 📅 Сегодня.
    Row 2: 🗓 Неделя, 🗓 Открыть неделю (batch CREATE, 5.26).
    Row 3: 💇 Услуги (entering_service flow).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Изменить окно", callback_data=AdminAddslotsCallbackData().pack())
    builder.button(text="📅 Сегодня", callback_data=AdminTodayCallbackData().pack())
    builder.button(text="🗓 Неделя", callback_data=AdminWeekCallbackData().pack())
    builder.button(text="🗓 Открыть неделю", callback_data=AdminOpenWeekEntryCallbackData().pack())
    builder.button(text="💇 Услуги", callback_data=AdminServicesCallbackData().pack())
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


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """Always-on reply keyboard для мастера (Session 5.62, пункт 5 от Екатерины).

    Екатерина жаловалась что inline menu уезжает вверх по чату — нужно скроллить
    или вводить /menu чтобы вернуть. Аналогично client_reply_keyboard (5.36 B.13)
    — делаем always-on reply keyboard снизу экрана.

    Layout: 3 кнопки на одном ряду (resize_keyboard=True shrink'нет до компактных
    кнопок после первого тапа, как в client_reply_keyboard).

    Кнопки:
    - 📋 Меню → cmd_menu (F.text match, StateFilter("*")) — escape hatch из любого
      FSM state, показывает inline menu с 7 actions в сообщении.
    - 📅 Сегодня → cmd_today (F.text match, StateFilter("*")) — список записей на
      сегодня. Read-only, безопасно чистит FSM state если admin был mid-flow.
    - 🗓 Неделя → cmd_week (F.text match, StateFilter("*")) — список записей на
      ближайшие 7 дней. Read-only, безопасно чистит FSM state.

    Остальные actions (/addslots, /openday, /openweek, /closeday, /services) —
    через inline menu (tap "📋 Меню" → inline keyboard в сообщении). Они требуют
    StateFilter(None) так как сами устанавливают FSM state.

    is_persistent=True — Telegram НЕ скрывает reply keyboard после первого тапа
    (поведение по умолчанию для старой admin_keyboard). Inline keyboard в
    сообщениях (calendar, slot picker) продолжает работать — reply keyboard
    не блокирует inline pickers.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📋 Меню"),
                KeyboardButton(text="📅 Сегодня"),
                KeyboardButton(text="🗓 Неделя"),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_today_keyboard(
    bookings: list[Booking],
    business_timezone: str = "Europe/Moscow",
    today_workday: WorkDay | None = None,
) -> InlineKeyboardMarkup:
    """Inline keyboard with [🔄 Перенести] button for each today booking (Этап 5.9).

    One button per booking, labeled with local time + service title (matches
    /today text line). admin taps → admin_move flow (calendar → 30-min slot
    picker → admin_move_booking service).

    Session 5.63 (пункт 3): adds [🔒 Закрыть день] button in a separate row
    IF today_workday is_active=True. Tap → admin_close_today_cb (inline confirm
    → close_workday_with_cancellations). Replaces the old "📅 Закрыть день"
    button that lived in admin_inline_menu (deleted in пункт 3).

    adjust(1) — one button per row (avoid horizontal clutter; Екатерина sees a
    list, not a grid). Telegram inline keyboard limit 100 buttons/row × N rows
    — pet-project single-tenant (Екатерина < 10 bookings/day), no pagination
    needed. If > 30 bookings — would need pagination (defer until pain).

    today_workday: WorkDay | None — today's WorkDay row. If is_active=True,
    a [🔒 Закрыть день] button is appended as a separate row. If None or
    is_active=False, no close button (nothing to close today).

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
    if today_workday is not None and getattr(today_workday, "is_active", False):
        builder.button(
            text="🔒 Закрыть день",
            callback_data=AdminCloseTodayCallbackData().pack(),
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


# ============================================================
# Этап 5.10 inline-часы: AdminWindow* callbacks + keyboards
# ============================================================


class AdminWindowSlot30CallbackData(CallbackData, prefix="admin_win30"):
    """30-min slot pick for /addslots inline window picker (Этап 5.10).

    Mirror AdminMoveSlot30CallbackData (prefix "admin_move_slot_30") but
    distinct prefix "admin_win30" — aiogram dispatch is exact-prefix match
    (callback_data.py:117-125), no conflict.

    Payload:
    - workday_id: UUID — WorkDay row (resolved in calendar_cb via select_workday).
      NON-Optional (mirror AdminMoveSlot30CallbackData:109). For /addslots inline
      open_workday uses master_id+work_date (NOT workday_id) but workday_id is
      kept in callback_data for state propagation symmetry.
    - start_minute: int — minutes since midnight (0-1439), encodes slot time_local.

    Wire format size: "admin_win30:<uuid>:<int>" ≈ 10+1+32+1+4 = 47 bytes < 64 limit.

    NB: same CallbackData class used for both "start" pick (mode="start") and
    "end" pick (mode="end"). The mode is determined by the StateFilter on the
    handler (picking_window_start vs picking_window_end), NOT by a field in
    callback_data. This keeps callback_data minimal and avoids mode-payload races.
    """

    workday_id: UUID
    start_minute: int


class AdminWindowConfirmCallbackData(CallbackData, prefix="admin_win_conf"):
    """Confirm window modify — final step in AdminStates.confirming_window (Этап 5.10).

    No payload (mirror AdminMoveConfirmCallbackData:113). Handler reads
    picked_start_minute + picked_end_minute + workday_id + selected_date from
    FSM state, NOT from callback payload — keeps callback_data small and avoids
    race where user could change FSM state mid-tap. Same pattern as
    AdminMoveConfirmCallbackData + admin_move_confirm_cb.
    """


# ============================================================
# Session 5.26: /openweek + /closeday callbacks + keyboards
# ============================================================


class AdminOpenWeekEntryCallbackData(CallbackData, prefix="admin_openweek_entry"):
    """Trigger /openweek flow from inline menu (Session 5.26).

    No payload — tap → picker start (admin_window_slot_picker_keyboard
    mode='start' БЕЗ booked_slots — новый день, не modify existing window).
    """


class AdminOpenWeekCallbackData(CallbackData, prefix="admin_openweek_days"):
    """Toggle weekday in /openweek flow (Session 5.26).

    Payload:
    - weekday: int 0-6 — Mon=0, Tue=1, ..., Sun=6 (Python date.weekday()).

    Wire format: "admin_openweek_days:<int>" ≈ 21+1+1 = 23 bytes < 64 limit.
    """

    weekday: int


class AdminOpenWeekNavCallbackData(CallbackData, prefix="admin_openweek_nav"):
    """Navigate between weeks in /openweek flow step 3 (Session 5.61).

    Payload:
    - delta: int — week offset change, -1 (prev) or +1 (next).

    Wire format: "admin_openweek_nav:<int>" ≈ 21+1+2 = 24 bytes < 64 limit.
    Distinct prefix from AdminOpenWeekCallbackData ("days" vs "nav") — no
    dispatch conflict.

    Handler (admin_openweek_week_nav_cb) updates week_offset in FSM state,
    resets selected_weekdays (week changed → old selection invalid), recomputes
    past_weekdays / scheduled_weekdays / closed_weekdays for the new monday.
    Alert "Выбор сброшен — новая неделя" signals the reset to the admin.
    """

    delta: int


class AdminOpenweekEditCallbackData(CallbackData, prefix="admin_openweek_edit"):
    """[✏️ Пн] inline button — start per-day window edit (Session 5.28 D).

    Payload:
    - weekday: int 0-6 — Mon=0..Sun=6 (for label rendering in tests).
    - work_date_iso: str YYYY-MM-DD — absolute date of the WorkDay to edit.

    Wire format: "admin_openweek_edit:<int>:<YYYY-MM-DD>" ≈ 21+1+1+10 = 33 bytes
    < 64 limit. Distinct prefix from AdminOpenWeekCallbackData ("days" vs
    "edit") — no dispatch conflict.

    work_date_iso prevents wrong-day-edit if user taps stale [✏️ Пн] from a
    previous week's summary: handler uses absolute date from callback_data,
    not _current_week_monday(tz) (which would resolve to current week's Mon).
    """

    weekday: int
    work_date_iso: str


class AdminCloseTodayCallbackData(CallbackData, prefix="admin_close_today"):
    """[🔒 Закрыть день] tap from admin_today_keyboard (Session 5.63, пункт 3).

    No payload — handler re-fetches today's WorkDay by business_tz (race-safe
    vs concurrent close via /closeday text or another admin tab).
    """


class AdminCloseTodayConfirmCallbackData(CallbackData, prefix="admin_close_today_confirm"):
    """[✅ Да, отменить записи] in today-close confirm step (Session 5.63, пункт 3).

    Carries workday_id in callback_data — no FSM state needed (race-safe vs
    state loss between confirm render and tap). Mirror AdminMoveConfirmCallbackData
    pattern (booking_id in callback_data, not state).
    """

    workday_id: str


class AdminCloseTodayCancelCallbackData(CallbackData, prefix="admin_close_today_cancel"):
    """[❌ Не закрывать] in today-close confirm step (Session 5.63, пункт 3).

    No payload — handler clears state (if any) and shows admin_inline_menu.
    """


@dataclass(frozen=True, slots=True)
class BookedSlot:
    """Active booking for picker filtering + header rendering (5.10 UX Variant A).

    Mirrors booking range in minutes since midnight (LOCAL time) for easy
    comparison with picker slots. client_name + service_title for header
    «🔒 Занято: HH:MM Имя (услуга)».

    Conversion Booking → BookedSlot done in handler (bot/handlers/admin.py)
    via _booking_to_booked_slot helper — keyboards don't touch DB/Booking model.
    """

    start_minute: int  # booking.start_at LOCAL in minutes since midnight
    end_minute: int  # booking.end_at LOCAL in minutes since midnight
    client_name: str  # snapshot, already html.escape()'d in booking.py
    service_title: str  # snapshot


@dataclass(frozen=True, slots=True)
class OpenedDay:
    """Successfully opened day in /openweek flow (Session 5.28 D).

    Collected by _apply_openweek for rendering summary text + edit keyboard.
    workday_id stored as str (UUID hex) — FSM state JSON-serialisable, picker
    callback_data payload also uses str (mirror admin_window_start_cb pattern).
    """

    weekday: int  # 0-6 (Mon..Sun) — key for [✏️ Пн] callback
    work_date_iso: str  # YYYY-MM-DD, for handler to recompute work_date
    workday_id: str  # UUID hex — for update_workday call in edit flow
    start_time_str: str  # "HH:MM" — for summary text
    end_time_str: str  # "HH:MM" — for summary text


def render_booked_header(booked_slots: list[BookedSlot]) -> str:
    """Render «🔒 Занято: HH:MM–HH:MM Имя (услуга)» header for picker (5.10 UX A).

    Returns empty string if no bookings. Caller (handlers/admin.py) prepends
    this to picker_text BEFORE showing keyboard — header is message text,
    not part of InlineKeyboardMarkup.

    Donor-standard (winnerxxx13/barbershop-telegram-bot booking.py:62-65):
    picker shows only free slots; header lists occupied ranges so admin sees
    where bookings are without scrolling.
    """
    if not booked_slots:
        return ""
    lines = ["🔒 <b>Занято:</b>"]
    for bs in booked_slots:
        start = _minute_to_time(bs.start_minute).strftime("%H:%M")
        end = _minute_to_time(bs.end_minute).strftime("%H:%M")
        lines.append(f"• {start}–{end} {bs.client_name} ({bs.service_title})")
    return "\n".join(lines) + "\n\n"


def admin_window_slot_picker_keyboard(
    workday_id: UUID,
    *,
    mode: str,
    business_tz: str = "Europe/Moscow",
    picked_start_minute: int | None = None,
    booked_slots: list[BookedSlot] | None = None,
) -> InlineKeyboardMarkup:
    """Inline 30-min slot picker for /addslots (window start/end) — Этап 5.10.

    /closeslot SHRINK inline flow REMOVED (5.10 simplification) — «Изменить
    окно» (mode="start" + mode="end") handles both shrink+extend+shift.

    5.10 UX Variant A (donor-standard, winnerxxx13/barbershop-telegram-bot):
    picker shows ONLY slots that don't cut existing bookings + caller renders
    «🔒 Занято: ...» header via render_booked_header() helper.

    Args:
        workday_id: UUID of the WorkDay being modified. Stored in callback_data
            payload so the next handler can resolve it without re-SELECT'ing.
        mode: "start" | "end" — determines which slots to render. The caller
            is responsible for setting the appropriate FSM state BEFORE showing
            this keyboard (handler dispatch by StateFilter).
        business_tz: IANA tz for slot labels (HH:MM in local time).
        picked_start_minute: required for mode="end" — start picked in previous
            step (admin_window_start_cb stored in FSM state, passed here to
            generate end slots starting from picked_start+30.
        booked_slots: active bookings for the workday (from
            get_active_bookings_for_workday, converted to BookedSlot list in
            handler). Slots overlapping a booking (start_minute <= slot <
            end_minute) rendered with 🔒 prefix and disabled callback
            "admin_window_booked" (handler shows alert "🔒 Занято ...").
            None or empty list → no bookings, all slots clickable.
            Bizarre-but-true: slots INSIDE booking range are shown as 🔒
            (user wants to SEE the booking in picker, msg 242).

    Slot ranges by mode (all slots are 30-min apart, label "HH:MM"):
        Business hours 09:00–20:00 (default work range for Екатерина):
        - mode="start": 540 (09:00), 570, ..., 1170 (19:30) — 22 start slots.
          Max start = 19:30 → end-picker shows single slot 20:00, non-empty.
        - mode="end": picked_start_minute+30, +60, ..., 1200 (20:00) — max
          end-slot = 20:00 (end_time = 20:30). Caller MUST pass picked_start_minute.
          If picked_start_minute >= 1200 → empty (shouldn't happen — start
          picker caps at 19:30 = 1170, so end always >= 1200 = 20:00).

    Edge case for rare early/late hours: /openday text command supports
    arbitrary HH:MM outside 09:00–20:00 (e.g. 08:00). Inline picker is
    optimised for the common 09:00–20:00 workday; full-range fallback via
    /openday.

    Empty list → single "Нет слотов" button (matches slot_picker_keyboard_30min
    UX in client.py:172). adjust(3) — 3 buttons per row.
    """
    builder = InlineKeyboardBuilder()
    tz = ZoneInfo(business_tz)

    # Compute candidate minutes by mode.
    # Business hours 09:00–20:00 (540–1200 min) — Екатерина's typical workday.
    if mode == "start":
        # 540, 570, ..., 1170 (09:00 → 19:30). max start = 19:30 = 1170 min.
        candidates = list(range(540, 1171, 30))  # 09:00..19:30 inclusive
    elif mode == "end":
        if picked_start_minute is None:
            raise ValueError("mode='end' requires picked_start_minute")
        # picked_start+30, +60, ..., 1200 (20:00). max end-slot = 20:00 = 1200.
        candidates = list(range(picked_start_minute + 30, 1201, 30))
    else:
        raise ValueError(f"unknown mode={mode!r}, expected 'start'|'end'")

    # msg 242 UX: picker показывает ВСЕ слоты, занятые с 🔒 prefix + disabled
    # callback. Заменяет donor-standard "filter out busy" (Variant A) —
    # пользователь хочет ВИДЕТЬ где занято прямо в picker, не только в header.
    if booked_slots:
        # Build a set of minutes that overlap any booking.
        # slot m is "busy" if m is inside [bs.start_minute, bs.end_minute) —
        # half-open: booking ending at 19:00 doesn't block slot 19:00.
        busy_minutes: set[int] = set()
        for bs in booked_slots:
            for m in range(bs.start_minute, bs.end_minute, 30):
                busy_minutes.add(m)
    else:
        busy_minutes = set()

    if not candidates:
        builder.button(text="Нет слотов", callback_data="noop")
        return builder.as_markup()

    # Build date from minute for label rendering (use today's date — only
    # HH:MM matters for label, date is irrelevant).
    from datetime import date as dt_date
    from datetime import datetime as dt_datetime

    ref_date = dt_date(2000, 1, 1)  # arbitrary, only .time() is used
    for minute in candidates:
        local_time = dt_datetime.combine(ref_date, _minute_to_time(minute), tzinfo=tz).time()
        label = local_time.strftime("%H:%M")
        if minute in busy_minutes:
            # 🔒 слот занят — показываем как disabled (callback="admin_window_booked").
            builder.button(text=f"🔒 {label}", callback_data="admin_window_booked")
        else:
            cb = AdminWindowSlot30CallbackData(
                workday_id=workday_id,
                start_minute=minute,
            )
            builder.button(text=label, callback_data=cb.pack())
    builder.adjust(3)
    return builder.as_markup()

    # Build date from minute for label rendering (use today's date — only
    # HH:MM matters for label, date is irrelevant).
    from datetime import date as dt_date
    from datetime import datetime as dt_datetime

    ref_date = dt_date(2000, 1, 1)  # arbitrary, only .time() is used
    for minute in candidates:
        local_time = dt_datetime.combine(ref_date, _minute_to_time(minute), tzinfo=tz).time()
        label = local_time.strftime("%H:%M")
        cb = AdminWindowSlot30CallbackData(
            workday_id=workday_id,
            start_minute=minute,
        )
        builder.button(text=label, callback_data=cb.pack())
    builder.adjust(3)
    return builder.as_markup()


def admin_window_confirm_keyboard() -> InlineKeyboardMarkup:
    """Build [✅ Подтвердить] / [❌ Отмена] keyboard for AdminStates.confirming_window (Этап 5.10).

    Mirror admin_move_confirm_keyboard() but uses AdminWindowConfirmCallbackData
    (distinct prefix "admin_win_conf", no conflict with "admin_move_confirm").
    Cancel button uses string "admin_window_cancel" — caught by F.data filter
    in admin_window_cancel_cb.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить",
        callback_data=AdminWindowConfirmCallbackData().pack(),
    )
    builder.button(text="❌ Отмена", callback_data="admin_window_cancel")
    builder.adjust(2)
    return builder.as_markup()


def _minute_to_time(minute: int) -> dt_time:
    """Convert minutes since midnight (0-1439) to datetime.time.

    Local helper for admin_window_slot_picker_keyboard label generation.
    Mirror client.py `dt_time(start_minute // 60, start_minute % 60)` pattern.
    """
    if not 0 <= minute <= 1439:
        raise ValueError(f"minute {minute} out of range 0-1439")
    return dt_time(minute // 60, minute % 60)


# ============================================================
# Session 5.26: /openweek + /closeday keyboards
# ============================================================

_WEEKDAY_LABELS: tuple[str, ...] = (
    "Пн",
    "Вт",
    "Ср",
    "Чт",
    "Пт",
    "Сб",
    "Вс",
)


def admin_week_days_keyboard(
    selected: set[int],
    past_weekdays: frozenset[int] = frozenset(),
    scheduled_weekdays: frozenset[int] = frozenset(),
    closed_weekdays: frozenset[int] = frozenset(),
    can_go_prev: bool = True,
    can_go_next: bool = True,
) -> InlineKeyboardMarkup:
    """7 toggle-кнопок дней недели + навигация по неделям + «✅ Открыть» + «❌ Отмена»
    (Session 5.26; 5.60 P2 — past_weekdays `❌` suffix; 5.61 — scheduled/closed
    weekday markers + week navigation).

    Args:
        selected: set of weekday ints (0=Mon..6=Sun) currently toggled ON.
            Toggle handler updates this set in FSM state and re-renders keyboard.
        past_weekdays: frozenset of weekday ints whose work_date < today_local.
            Past days get ` ❌` suffix on label (variant A — minimal fix).
            callback_data stays unchanged so admin can still tap → toggle
            (apply filters past days at /openweek confirm: admin.py:3079-3081).
            Default empty for backward compat (no past days in fresh week).
        scheduled_weekdays: frozenset of weekday ints with an active WorkDay
            (is_active=True). Gets ` 🟡` suffix — visual signal "day has window,
            will be overwritten". 5.61.
        closed_weekdays: frozenset of weekday ints with a closed WorkDay
            (is_active=False). Gets ` ⚪` suffix — "day was open then closed via
            /closeday, re-open action". 5.61.
        can_go_prev: show «← Пред.» button. False when week_offset=0 (current
            week — prev week is fully in past, no point navigating there).
        can_go_next: show «След. →» button. False when week_offset >= MAX
            (4 weeks ahead cap).

    Suffix priority: ` ❌` (past) > ` 🟡` (active WorkDay) > ` ⚪` (closed WorkDay).
    Past day with WorkDay → ` ❌` wins (apply will filter it anyway, no point
    showing 🟡/⚪). Future day with active WorkDay → ` 🟡`. Future day with
    closed WorkDay → ` ⚪`.

    Layout: 7 weekday buttons (row 1, adjust(7) compresses to ≤8/row Telegram
    inline limit 8 buttons/row), then nav row (← Пред. / След. →, adjust(2)),
    then [✅ Открыть] + [❌ Отмена] row (adjust(2)).

    Selected weekdays помечены ✅ prefix; unselected — без prefix.
    «✅ Открыть» callback_data="admin_openweek_confirm" (string).
    «❌ Отмена» callback_data="admin_openweek_cancel" (string).
    """
    builder = InlineKeyboardBuilder()
    for weekday in range(7):
        label = _WEEKDAY_LABELS[weekday]
        prefix = "✅ " if weekday in selected else ""
        if weekday in past_weekdays:
            past_suffix = " ❌"
        elif weekday in scheduled_weekdays:
            past_suffix = " 🟡"
        elif weekday in closed_weekdays:
            past_suffix = " ⚪"
        else:
            past_suffix = ""
        builder.button(
            text=f"{prefix}{label}{past_suffix}",
            callback_data=AdminOpenWeekCallbackData(weekday=weekday).pack(),
        )
    nav_row: list[InlineKeyboardButton] = []
    if can_go_prev:
        nav_row.append(
            InlineKeyboardButton(
                text="← Пред.",
                callback_data=AdminOpenWeekNavCallbackData(delta=-1).pack(),
            )
        )
    if can_go_next:
        nav_row.append(
            InlineKeyboardButton(
                text="След. →",
                callback_data=AdminOpenWeekNavCallbackData(delta=1).pack(),
            )
        )
    if nav_row:
        builder.row(*nav_row)
    builder.button(text="✅ Открыть", callback_data="admin_openweek_confirm")
    builder.button(text="❌ Отмена", callback_data="admin_openweek_cancel")
    builder.adjust(7, 2)
    return builder.as_markup()


def admin_openweek_overwrite_keyboard(
    button_text: str = "✅ Да, перезаписать",
) -> InlineKeyboardMarkup:
    """[✅ Да, ...] / [❌ Нет, отмена] keyboard for /openweek confirm step
    (Session 5.27 B; 5.60 — button_text parameter for re-open vs overwrite).

    Triggered when master taps [✅ Открыть] but some selected days already have
    WorkDay rows. Without this guard, open_workday UPCERT would silently
    overwrite existing windows (data loss risk — master forgot week was open).

    5.60 — заголовок alert и текст «Да» зависят от статуса существующих дней:
    - all_active → «✅ Да, перезаписать» (overwrite existing window)
    - all_closed → «✅ Да, открыть» (re-open closed day)
    - mixed     → «✅ Да, открыть/перезаписать» (both actions)

    callback_data is the same for all variants — yes-handler reads FSM state,
    not button text, so re-open and overwrite share the same apply path.

    Args:
        button_text: label for the confirm button. Default "✅ Да, перезаписать"
            for backward compat with all-active case.

    «✅ Да» callback_data="admin_openweek_overwrite_yes" (string).
    «❌ Нет» callback_data="admin_openweek_overwrite_no" (string).
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=button_text,
        callback_data="admin_openweek_overwrite_yes",
    )
    builder.button(text="❌ Нет, отмена", callback_data="admin_openweek_overwrite_no")
    builder.adjust(2)
    return builder.as_markup()


def admin_openweek_edit_keyboard(opened_days: list[OpenedDay]) -> InlineKeyboardMarkup:
    """[✏️ Пн] [✏️ Вт] ... [✅ Готово] — per-day window edit after /openweek
    apply (Session 5.28 D).

    One [✏️ <Day>] button per opened day (sorted by weekday). [✅ Готово]
    below to exit edit flow (state.clear + /menu).

    Args:
        opened_days: list of OpenedDay (only successfully opened — failed
            days don't get an edit button, no WorkDay to update).

    Layout: 7 [✏️] buttons in adjust(7), then [✅ Готово] alone. Telegram
    inline limit 8 buttons/row, adjust(7, 1) packs weekdays into one row.
    """
    builder = InlineKeyboardBuilder()
    for od in sorted(opened_days, key=lambda d: d.weekday):
        day_label = _WEEKDAY_LABELS[od.weekday]
        builder.button(
            text=f"✏️ {day_label}",
            callback_data=AdminOpenweekEditCallbackData(
                weekday=od.weekday,
                work_date_iso=od.work_date_iso,
            ).pack(),
        )
    builder.button(text="✅ Готово", callback_data="admin_openweek_done")
    builder.adjust(7, 1)
    return builder.as_markup()


def admin_close_today_confirm_keyboard(workday_id: UUID) -> InlineKeyboardMarkup:
    """[✅ Да, отменить записи] / [❌ Не закрывать] keyboard for today-view
    close confirm (Session 5.63, пункт 3).

    Uses AdminCloseTodayConfirmCallbackData(workday_id) — no FSM state needed.
    workday_id is injected into callback_data so confirm handler can fetch the
    WorkDay without reading state (race-safe vs state loss).

    workday_id: UUID of the WorkDay to close (today's active WorkDay).
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Да, отменить записи",
        callback_data=AdminCloseTodayConfirmCallbackData(workday_id=str(workday_id)).pack(),
    )
    builder.button(
        text="❌ Не закрывать",
        callback_data=AdminCloseTodayCancelCallbackData().pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


def admin_week_picker_keyboard(
    can_go_prev: bool,
    can_go_next: bool,
) -> InlineKeyboardMarkup:
    """Week picker keyboard for /openweek step 0 (Session 5.64, пункт 1).

    nav row: [← Пред.] [След. →] (uses AdminOpenWeekNavCallbackData — same
    callback as step 3, dispatched by StateFilter to a different handler).
    row 2: [✅ Выбрать эту неделю] (string "admin_openweek_week_select").
    row 3: [❌ Отмена] (string "admin_openweek_cancel" — caught by
    admin_openweek_cancel_cb which uses StateFilter(AdminStates)).

    No weekday buttons — week picker is a single-selection step (user picks
    a week via nav, then taps "Выбрать" to confirm). Selected week is tracked
    in FSM state `week_offset` (updated by nav handler).

    Args:
        can_go_prev: show «← Пред.» button. False when week_offset=0 (current
            week — prev week is fully in past, no point navigating there).
        can_go_next: show «След. →» button. False when week_offset >=
            _OPENWEEK_MAX_OFFSET (4 weeks ahead cap).
    """
    builder = InlineKeyboardBuilder()
    nav_row: list[InlineKeyboardButton] = []
    if can_go_prev:
        nav_row.append(
            InlineKeyboardButton(
                text="← Пред.",
                callback_data=AdminOpenWeekNavCallbackData(delta=-1).pack(),
            )
        )
    if can_go_next:
        nav_row.append(
            InlineKeyboardButton(
                text="След. →",
                callback_data=AdminOpenWeekNavCallbackData(delta=1).pack(),
            )
        )
    if nav_row:
        builder.row(*nav_row)
    builder.button(text="✅ Выбрать эту неделю", callback_data="admin_openweek_week_select")
    builder.button(text="❌ Отмена", callback_data="admin_openweek_cancel")
    builder.adjust(1)
    return builder.as_markup()
