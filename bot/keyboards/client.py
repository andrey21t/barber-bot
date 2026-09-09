"""Inline keyboards for booking flow — date picker, slot picker, confirm.

Date picker (Session 5.28 — BB-110): tap-to-select flat list of bookable dates
(``date_picker_keyboard`` + ``BookDateCallbackData``) replaces the full-month
SimpleCalendar in /book and /slots — single-master schedule is sparse, month
grid mostly surfaced "Мастер не работает в этот день" (PLANS.md:827).
SimpleCalendar (``calendar_keyboard``) REMAINS for the /transfer flow (re-uses
SimpleCalendarCallback picker, distinct FSM state) and for stale-keyboard
retries posted before the 5.28 deploy.

CallbackData factories (aiogram 3.x):
- BookSlotCallbackData: prefix="book_slot", slot_id: UUID
- BookConfirmCallbackData: prefix="book_confirm"
- BookCancelCallbackData: prefix="book_cancel"  (booking flow cancel — no payload)
- MyBookingsCancelCallbackData: prefix="mybook_cancel", booking_id: UUID  (cancel existing booking)
- MyBookingsTransferCallbackData: prefix="mybook_transfer", booking_id: UUID
  (transfer existing booking — re-uses SimpleCalendar picker in subsequent FSM steps)
- BookDateCallbackData: prefix="book_date", work_date: ISO date str (BB-110)

Note: prefix uses '_' not ':' — aiogram 3.x forbids separator ':' inside prefix
(ValueError: "Separator symbol ':' can not be used inside prefix").
"""

from datetime import UTC, date, datetime
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


class BookDateCallbackData(CallbackData, prefix="book_date"):
    """Date picker callback (Session 5.28 — BB-110, replaces SimpleCalendar).

    Payload: work_date as ISO string ("YYYY-MM-DD"). aiogram CallbackData
    pack() forbids ':' inside values (verified aiogram 3.x source — same
    constraint as BookSlot30CallbackData.start_minute); ISO date has only
    '-' separators → pack() safe. Handler parses via date.fromisoformat.

    Wire format: "book_date:2026-09-08" = 9+1+10 = 20 bytes < 64 limit.

    Distinct prefix from booking flow callbacks (book_slot, book_slot_30,
    book_service) — aiogram dispatch is exact-prefix match, no conflict.
    Shared by /book and /slots flows (both set BookingStates.selecting_date;
    handler branches on the is_slots_path FSM flag, same as the legacy
    simple_calendar_cb).

    Cancel is a plain string "book_date_cancel" (no payload) caught by
    F.data == "book_date_cancel" filter — mirrors the "book_service_custom"
    pattern (BookServiceCallbackData docstring).
    """

    work_date: str


class ClientMenuBookCallbackData(CallbackData, prefix="client_book"):
    """Client /start menu — single [💇 Записаться] button (2026-09-06 fix).

    Entry point for clients: tap → handler sets BookingStates.selecting_date
    + shows calendar (same flow as /book, but triggered from inline menu
    instead of text command). Solves "client sees empty chat after /start"
    — before this, /start replied with bare text "Запишитесь командой /book"
    and clients without bot experience didn't know what to do.

    Distinct prefix from booking flow callbacks (book_slot, book_slot_30,
    book_service, book_date) — aiogram dispatch is exact-prefix match.
    Plain prefix (no payload) — same pattern as BookConfirmCallbackData.

    Also re-used by post_booking_keyboard [💇 Ещё запись] button (2026-09-06
    Session 6 — Task 1): after a successful booking, the client taps this
    button to start another booking without typing /book. StateFilter(None)
    on client_book_cb allows entry from the just-cleared confirm_cb state.
    """


class ClientMenuMyBookingsCallbackData(CallbackData, prefix="client_mybookings"):
    """Post-booking [📋 Мои записи] button (Session 6 — Task 1, 2026-09-06).

    Used by post_booking_keyboard to give the client a one-tap way to view
    their existing bookings after a successful confirm. Tap → handler
    re-uses the same _render_mybookings helper as /mybookings (shows list +
    [❌ Отменить] / [🔄 Перенести] buttons for cancelable bookings).

    Distinct prefix from booking flow callbacks (book_*, mybook_*) — aiogram
    dispatch is exact-prefix match. Plain prefix (no payload) — same pattern
    as ClientMenuBookCallbackData / BookConfirmCallbackData.

    Why a NEW CallbackData instead of reusing MyBookingsCancelCallbackData:
    the latter carries a booking_id payload and triggers cancellation, not
    list rendering. Reusing it for "show my list" would collide semantically
    with the cancel handler's filter and confuse aiogram dispatch.
    """


def client_inline_menu() -> InlineKeyboardMarkup:
    """Single-button inline menu for client /start (2026-09-06 fix).

    Layout: 1 button [💇 Записаться] — starts /book flow via callback
    (ClientMenuBookCallbackData). Kept minimal per user request:
    "клиент зашёл — сразу кнопка для записи, без «О нас» / «Контакты»".

    If later we add more client actions (e.g. 📋 Мои записи, 📞 Контакты),
    extend here with more buttons + adjust() layout.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="💇 Записаться", callback_data=ClientMenuBookCallbackData().pack())
    return builder.as_markup()


def post_booking_keyboard() -> InlineKeyboardMarkup:
    """Two-button inline menu shown after a successful booking (Session 6 — Task 1).

    Layout: 1 row, 2 buttons:
      - [📋 Мои записи] → ClientMenuMyBookingsCallbackData → re-renders the
        /mybookings list (cancelable bookings get [❌ Отменить] buttons).
      - [💇 Ещё запись]  → ClientMenuBookCallbackData → starts a new /book
        flow (state.clear already done in confirm_cb, StateFilter(None) OK).

    Why both buttons unconditionally (no has_bookings flag): the mybookings
    handler already says "У вас нет активных записей" when the list is empty,
    so showing [📋 Мои записи] after a successful booking is never misleading
    (we just created one — list is non-empty). Skipping a DB count lookup
    here keeps the keyboard builder pure (no session needed) and matches
    the Pure/I-O contract of keyboard helpers (no side effects, no DB).

    adjust(2) — both buttons on the same row (Telegram renders side-by-side
    on desktop, stacked on narrow mobile — both layouts are readable).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Мои записи", callback_data=ClientMenuMyBookingsCallbackData().pack())
    builder.button(text="💇 Ещё запись", callback_data=ClientMenuBookCallbackData().pack())
    builder.adjust(2)
    return builder.as_markup()


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


WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

DATE_PICKER_CANCEL_CB = "book_date_cancel"


def _date_button_label(d: date, today: date) -> str:
    """Human label for a date button: 'Пн 08.09' / '⚡ Сегодня, Пн 08.09'.

    Weekday prefix gives enough context without a year (picker window is
    <= 60 days ahead). 'Сегодня' marker makes the edge case (booking for
    today) discoverable — slots for today are still filtered by past time
    in get_available_slots_30, so the marker can't mislead.
    """
    wd = WEEKDAYS_RU[d.weekday()]
    if d == today:
        return f"⚡ Сегодня, {wd} {d.strftime('%d.%m')}"
    return f"{wd} {d.strftime('%d.%m')}"


def date_picker_keyboard(dates: list[date], today: date) -> InlineKeyboardMarkup:
    """Build tap-to-select date picker (Session 5.28 — BB-110).

    Replaces SimpleCalendar in /book and /slots: single-master schedule is
    sparse (1-3 working days/week), so a flat list of bookable dates is
    better UX than a month grid where most taps surfaced "Мастер не
    работает в этот день" (PLANS.md:827). Pattern mirrors the 5.27 service
    picker (tap-to-select CallbackData + last-row action buttons).

    Args:
        dates: bookable dates from get_bookable_dates (sorted ascending by
            the service — labels render in the given order).
        today: today's date in the business timezone — used for the
            '⚡ Сегодня' marker only.

    Returns:
        InlineKeyboardMarkup — date buttons (2 per row) + last row
        ['❌ Отмена'] (callback DATE_PICKER_CANCEL_CB). Empty dates list
        renders a single disabled 'Нет свободных дат' button before cancel —
        callers that pre-check emptiness (cmd_book) show a text-only empty
        state instead; this defensive branch covers retry paths where all
        dates got booked between render and tap (race).

    Built with explicit InlineKeyboardButton rows (not
    InlineKeyboardBuilder.adjust) — guarantees cancel lands on its own row
    instead of pairing with the last date under adjust(2) (matches mybookings
    UX where action buttons sit in their own row).
    """
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for d in dates:
        pair.append(
            InlineKeyboardButton(
                text=_date_button_label(d, today),
                callback_data=BookDateCallbackData(work_date=d.isoformat()).pack(),
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if not dates:
        rows.append([InlineKeyboardButton(text="Нет свободных дат", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=DATE_PICKER_CANCEL_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slot_picker_keyboard(
    slots: list[Slot],
    *,
    show_back: bool = True,
) -> InlineKeyboardMarkup:
    """Build inline keyboard with available slots.

    DEPRECATED (Этап 5.4): kept for the legacy slot-based /book flow until
    5.8 introduces /slots command + WorkDay-based booking. New code should
    use `slot_picker_keyboard_30min` which renders TimeSlot30 buttons with
    "HH:MM" labels (30-min step grid from WorkDay).

    Each button shows slot_hour (e.g. "14:00"), callback_data carries slot UUID.
    Empty slots list → "Нет свободных слотов" placeholder + optional "↩️ Назад".

    Session 5.30 (S1): added "↩️ Назад" (callback_data="book_back_to_service")
    for UX consistency with `slot_picker_keyboard_30min` — /book users with
    legacy slots can now return to the service picker without /cancel restart.

    Session 5.31 (S1 review fix F2): `show_back=False` suppresses the back
    button — used by the transfer flow (`_process_selected_date` with
    `is_transfer=True`) where "back to service" has no meaning (transfer has
    no service step). The booking flow uses the default `show_back=True`.

    Args:
        slots: list of bot.models.Slot.
        show_back: when True, append "↩️ Назад" (callback_data="book_back_to_service")
            on the last row; when False, render slot buttons only. Default True
            for backward-compat with existing booking callers.
    """
    builder = InlineKeyboardBuilder()
    if not slots:
        builder.button(text="Нет свободных слотов", callback_data="noop")
        if show_back:
            builder.button(text="↩️ Назад", callback_data="book_back_to_service")
        return builder.as_markup()

    for slot in slots:
        cb = BookSlotCallbackData(slot_id=slot.id)
        label = f"{slot.slot_hour:02d}:00"
        builder.button(text=label, callback_data=cb.pack())
    if show_back:
        builder.button(text="↩️ Назад", callback_data="book_back_to_service")
    builder.adjust(3)  # 3 slots per row
    return builder.as_markup()


def slot_picker_keyboard_30min(
    slots: list[TimeSlot30],
    workday_id: UUID,
    *,
    show_back: bool = True,
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

    Builder MUST pass `workday_id` (resolved in simple_calendar_cb slots branch
    via `_select_workday_for_slot` or equivalent — single source of truth).

    Session 5.30 (S1): last row adds "↩️ Назад" (callback_data="book_back_to_service")
    to let user return to the service picker without /cancel + /book restart.

    Session 5.31 (S1 review fix F2): `show_back=False` suppresses the back
    button — used by the transfer flow (`_process_selected_date` with
    `is_transfer=True`) where "back to service" has no meaning (transfer has
    no service step). The booking flow uses the default `show_back=True`.

    Args:
        slots: list of TimeSlot30 (pre-formatted labels, business-tz).
        workday_id: UUID of the WorkDay the slots belong to (single source of truth).
        show_back: when True, append "↩️ Назад" (callback_data="book_back_to_service")
            on the last row; when False, render slot buttons only. Default True
            for backward-compat with existing booking callers.
    """
    builder = InlineKeyboardBuilder()
    if not slots:
        builder.button(text="Нет свободных слотов", callback_data="noop")
        if show_back:
            builder.button(text="↩️ Назад", callback_data="book_back_to_service")
        return builder.as_markup()

    for slot in slots:
        start_minute = slot.start_time_local.hour * 60 + slot.start_time_local.minute
        cb = BookSlot30CallbackData(
            workday_id=workday_id,
            start_minute=start_minute,
        )
        builder.button(text=slot.label, callback_data=cb.pack())
    if show_back:
        builder.button(text="↩️ Назад", callback_data="book_back_to_service")
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

    Session 5.30 (S1): last row adds "↩️ Назад" (callback_data="book_back_to_date")
    to let user return to the date picker without /cancel + /book restart.

    Empty services list is handled by the caller (name_msg shows text prompt
    instead when no services in DB — single-master MVP, rare case).

    Args:
        services: list of bot.models.Service (active, business-scoped).

    Returns:
        InlineKeyboardMarkup — buttons in 2 columns (adjust(2)), custom
        "✏️ Своя услуга" + "↩️ Назад" appended last. With **even** service
        count the back button pairs with custom on the last row (adjust(2)
        groups globally); with **odd** count it lands on its own row.
    """
    builder = InlineKeyboardBuilder()
    for svc in services:
        cb = BookServiceCallbackData(service_id=svc.id)
        builder.button(text=svc.name, callback_data=cb.pack())
    builder.button(text="✏️ Своя услуга", callback_data="book_service_custom")
    builder.button(text="↩️ Назад", callback_data="book_back_to_date")
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

    B.1: workday-only bookings (b.slot_id is None) now show [🔄 Перенести] —
    transfer_booking workday-path implemented (booking.py, B.1). Button always
    shown for all cancelable bookings (slot-based and workday-based).
    adjust(2) → even rows (2 buttons per booking).
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


# ============================================================
# Session 5.36 (B.13) — Reply keyboard + pre-fill name callbacks
# ============================================================

# Text labels for the client reply keyboard buttons. Handlers match on
# F.text == CLIENT_REPLY_BOOK_LABEL / F.text == CLIENT_REPLY_MYBOOKINGS_LABEL
# (registered in client.py). Master (ADMIN_ID) does NOT see this keyboard —
# cmd_start branches on settings.ADMIN_ID and shows admin_inline_menu instead.
CLIENT_REPLY_BOOK_LABEL = "💇 Записаться"
CLIENT_REPLY_MYBOOKINGS_LABEL = "📋 Мои записи"

# Session 5.46 (B.10) — Phone collection reply keyboard labels.
# Handlers match on F.text == PHONE_SKIP_LABEL (registered in client.py BEFORE
# phone_msg so the dispatcher routes the skip text here, not into the
# normalize_phone path). PHONE_SHARE_LABEL is the text on the request_contact
# button — Telegram sends a Contact object on tap (NOT a text message), so the
# share button is dispatched via F.content_type == ContentType.CONTACT.
PHONE_SHARE_LABEL = "📱 Поделиться"
PHONE_SKIP_LABEL = "⏭ Без телефона"


class NamePreFillYesCallbackData(CallbackData, prefix="name_prefill_yes"):
    """Pre-fill name: client tapped [✅ Да, это я] (Session 5.36 / B.13).

    No payload — handler reads from_user.first_name from the callback (already
    shown in the prompt text "Записать на {name}?"). Same pattern as
    BookConfirmCallbackData (plain prefix, no payload).

    Distinct prefix from booking flow callbacks (book_slot, book_slot_30,
    book_service, book_date) — aiogram dispatch is exact-prefix match.
    """


class NamePreFillOtherCallbackData(CallbackData, prefix="name_prefill_other"):
    """Pre-fill name: client tapped [👤 Другое имя] (Session 5.36 / B.13).

    No payload — handler just transitions FSM to entering_name (text input).
    Same pattern as NamePreFillYesCallbackData.

    Distinct prefix from name_prefill_yes and booking flow callbacks —
    aiogram dispatch is exact-prefix match.
    """


def client_reply_keyboard() -> ReplyKeyboardMarkup:
    """Build the always-on reply keyboard for clients (Session 5.36 / B.13).

    Two buttons, always visible at the bottom of the chat (like a phone
    keyboard — Telegram renders reply keyboard as part of the UI, not inside
    a message). Solves "client doesn't see where to tap after /start" —
    buttons are always there, no need to scroll up to the first message.

    Layout: 2 buttons on one row (resize_keyboard=True keeps buttons compact
    after first tap — Telegram shrinks them to one line by default).

    Args:
        None — keyboard is static (no DB, no session). Matches the Pure/I-O
        contract of other keyboard helpers.

    Returns:
        ReplyKeyboardMarkup with 2 KeyboardButtons:
        - 💇 Записаться → triggers reply_book_msg (F.text match in client.py)
        - 📋 Мои записи → triggers reply_mybookings_msg (F.text match)

    NB: NOT shown to master (ADMIN_ID). cmd_start branches: master gets
    admin_inline_menu + ReplyKeyboardRemove, client gets this reply keyboard.
    The 2 reply-keyboard handlers (reply_book_msg / reply_mybookings_msg)
    also guard with `message.from_user.id != settings.ADMIN_ID` as
    defense-in-depth (in case a stale reply keyboard from a pre-B.13 session
    lingers on master's device).
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=CLIENT_REPLY_BOOK_LABEL),
                KeyboardButton(text=CLIENT_REPLY_MYBOOKINGS_LABEL),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def name_pre_fill_keyboard(first_name: str) -> InlineKeyboardMarkup:
    """Build [✅ Да, это я] / [👤 Другое имя] inline keyboard (Session 5.36 / B.13).

    Shown after slot selection when from_user.first_name is non-empty. Lets
    the client confirm their Telegram profile name in one tap (80% case —
    booking themselves) OR switch to text input (20% case — booking someone
    else: child, husband, etc.).

    Args:
        first_name: the client's Telegram first_name (already shown in the
            prompt text "Записать на {first_name}?"). NOT stored in callback
            data — handler reads it from callback.from_user.first_name again
            (keeps callback_data minimal, avoids stale-name race).

    Returns:
        InlineKeyboardMarkup with 2 buttons on one row (adjust(2)).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, это я", callback_data=NamePreFillYesCallbackData().pack())
    builder.button(text="👤 Другое имя", callback_data=NamePreFillOtherCallbackData().pack())
    builder.adjust(2)
    return builder.as_markup()


# ============================================================
# Session 5.46 (B.10) — Phone collection reply keyboard
# ============================================================
def phone_keyboard() -> ReplyKeyboardMarkup:
    """Build the phone-share reply keyboard (Session 5.46 / B.10).

    Two buttons on one row:
    - [📱 Поделиться] with ``request_contact=True`` — Telegram native share.
      On tap, Telegram sends a Message with ``content_type == ContentType.CONTACT``
      and ``message.contact.phone_number`` populated. The share_contact_msg
      handler (client.py) catches it via F.content_type == ContentType.CONTACT
      + StateFilter(BookingStates.entering_phone).
    - [⏭ Без телефона] — plain KeyboardButton. On tap, Telegram sends a text
      message with the button text. The phone_skip_msg handler catches it via
      F.text == PHONE_SKIP_LABEL (registered BEFORE phone_msg so the dispatcher
      routes the skip text here, not into the normalize_phone path).

    Layout: 2 buttons on one row (resize_keyboard=True keeps them compact after
    first tap). is_persistent=True so the keyboard stays visible until the user
    picks an option (matches client_reply_keyboard from B.13).

    NB: /cancel works from entering_phone via the global cancel_msg handler
    (StateFilter("*")) — no Cancel button on this keyboard to keep it minimal.
    The user can always type /cancel or just /cancel through Telegram command
    autocomplete.

    Args:
        None — keyboard is static (no DB, no session).

    Returns:
        ReplyKeyboardMarkup with 2 KeyboardButtons (one row). The Share
        button has ``request_contact=True``, the Skip button is a plain
        KeyboardButton.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=PHONE_SHARE_LABEL, request_contact=True),
                KeyboardButton(text=PHONE_SKIP_LABEL),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
