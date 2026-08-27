"""Master/admin commands + inline-menu callbacks — Pure I/O layer.

Contract (spec.md 200-213, 251, 307-309):
- Auth: F.from_user.id == settings.ADMIN_ID (MVP single-master). TODO Ур. 2.6:
  extract to middleware/role.py (spec 258) — DB lookup current_business, current_master.
  _is_admin — для Message, _is_admin_callback — для CallbackQuery (callback.from_user).
- Stateful: YES — 5 command handlers (/addslots /closeslot /today /week /services)
  run StateFilter(None) (no FSM). 5 inline-menu callbacks (Этап 1.3a) — StateFilter("*"),
  стартуют FSM-потоки через state.set_state(AdminStates.*). FSM addslots (1.3b) —
  calendar handler + hours message handler, переход calendar→hours через edit_message_text
  (INL-001). FSM closeslot (1.3c) — TODO. FSM services (1.3d) — TODO.
- 5 command handlers + 5 inline-menu callbacks + 2 addslots FSM handlers (1.3b).
- Pure I/O: parse args, call service, render result. NO business logic here.
- HTML escape: client_name_snapshot already escaped in DB (booking.py), render
  WITHOUT re-escape (Telegram parse_mode=HTML). Newlines in snapshot are
  replaced with space in _render_bookings (html.escape(quote=False) skips \n).
- Timezone: today/week use business.timezone (from DB), NOT UTC. slot.slot_date is LOCAL.
- slot_date validation: /addslots rejects past dates (spec.md 401, slot_date >= today_local).
"""

import html
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram_calendar.schemas import SimpleCalAct
from sqlalchemy.exc import SQLAlchemyError

from bot.config import get_settings
from bot.db import async_session_factory
from bot.keyboards.admin import (
    AdminAddslotsCallbackData,
    AdminCloseslotCallbackData,
    AdminServicesCallbackData,
    AdminTodayCallbackData,
    AdminWeekCallbackData,
    admin_calendar_keyboard,
)
from bot.models import Booking
from bot.services.admin import (
    create_service,
    get_today_bookings,
    get_week_bookings,
)
from bot.services.slots import (
    SlotAlreadyExistsError,
    add_slots,
    close_slot,
)
from bot.states import AdminStates

logger = logging.getLogger(__name__)

router = Router(name="admin")


# ============================================================
# Helpers (DB lookups, kept here for MVP — TODO Ур. 2.6: middleware)
# ============================================================
async def _resolve_master_and_business(telegram_id: int) -> tuple[UUID, UUID, str] | None:
    """Return (master_id, business_id, business_timezone) or None.

    Single-master MVP: master by telegram_id, business via FK.
    TODO Ур. 2.6: move to role middleware, inject into workflow_data.
    """
    from sqlalchemy import select

    from bot.models import Business, Master

    async with async_session_factory() as session:
        stmt_m = select(Master).where(Master.telegram_id == telegram_id).limit(1)
        master = (await session.execute(stmt_m)).scalar_one_or_none()
        if master is None:
            return None
        stmt_b = select(Business).where(Business.id == master.business_id).limit(1)
        business = (await session.execute(stmt_b)).scalar_one_or_none()
        if business is None:
            return None
        return master.id, business.id, business.timezone


def _is_admin(message: Message) -> bool:
    """MVP auth: single-master check against settings.ADMIN_ID.

    TODO Ур. 2.6: replace with middleware/role.py (spec 258).
    """
    settings = get_settings()
    user = message.from_user
    return user is not None and user.id == settings.ADMIN_ID


def _is_admin_callback(callback: CallbackQuery) -> bool:
    """Auth for CallbackQuery — parallel to _is_admin for Message.

    _is_admin проверяет `message.from_user` — для CallbackQuery это БОТ
    (callback.message.from_user = bot), НЕ тапер. Здесь нужен
    `callback.from_user` — кто нажал кнопку.

    TODO Ур. 2.6: заменить на middleware/role.py (spec 258).
    """
    settings = get_settings()
    user = callback.from_user
    return user is not None and user.id == settings.ADMIN_ID


def _require_admin_or_silent(message: Message) -> int | None:
    """Return admin telegram_id if admin, else None (caller returns silently).

    Single helper for handlers — covers _is_admin + user.id narrowing in one call.
    """
    if not _is_admin(message):
        return None
    return message.from_user.id if message.from_user is not None else None


# ============================================================
# 1. /addslots <YYYY-MM-DD> <h1> <h2> ...
# ============================================================
@router.message(Command("addslots"), StateFilter(None))
async def cmd_addslots(message: Message, command: CommandObject) -> None:
    """Open slots on a date for master.

    Parse: `addslots 2026-03-17 11 12 13 14 15`
    Idempotent — skips already-existing slots (service handles).
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Формат: <code>/addslots ГГГГ-ММ-ДД ЧАС1 ЧАС2 ...</code>\n"
            "Пример: <code>/addslots 2026-03-17 11 12 13 14</code>"
        )
        return

    iso = args[0]
    try:
        slot_date = date.fromisoformat(iso)
    except ValueError:
        await message.answer("❌ Неверная дата. Формат: ГГГГ-ММ-ДД (например 2026-03-17)")
        return

    # Parse hours + dedup (composite UNIQUE on (master_id, slot_date, slot_hour)
    # will reject duplicate hours in same request if not deduped here).
    try:
        hours_raw = [int(h) for h in args[1:]]
    except ValueError:
        await message.answer("❌ Часы должны быть числами 0-23 (например 11 12 13)")
        return

    hours = sorted(set(hours_raw))  # dedup

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, tz = resolved

    # spec.md 401: slot_date must not be in the past (LOCAL today in business.timezone).
    today_local = datetime.now(ZoneInfo(tz)).date()
    if slot_date < today_local:
        await message.answer(
            f"❌ Нельзя создать слот в прошлом. Сегодня: {today_local.strftime('%d.%m.%Y')}"
        )
        return

    async with async_session_factory() as session:
        try:
            created = await add_slots(session, master_id, slot_date, hours)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        except SlotAlreadyExistsError:
            await message.answer("❌ Один из слотов уже существует (гонка). Попробуйте ещё раз.")
            return

    if created:
        hours_str = ", ".join(str(s.slot_hour) for s in created)
        await message.answer(
            f"✅ Открыты слоты на {slot_date.strftime('%d %B')}:\n<b>{hours_str}</b>"
        )
    else:
        await message.answer(
            f"Все слоты на {slot_date.strftime('%d %B')} уже открыты — ничего не добавлено."
        )


# ============================================================
# 2. /closeslot <YYYY-MM-DD> <hour>
# ============================================================
@router.message(Command("closeslot"), StateFilter(None))
async def cmd_closeslot(message: Message, command: CommandObject) -> None:
    """Close a slot. Refuses if slot has an active booking.

    Parse: `closeslot 2026-03-17 14`
    """
    if not _is_admin(message):
        return

    args = (command.args or "").split()
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/closeslot ГГГГ-ММ-ДД ЧАС</code>\n"
            "Пример: <code>/closeslot 2026-03-17 14</code>"
        )
        return

    iso = args[0]
    try:
        slot_date = date.fromisoformat(iso)
    except ValueError:
        await message.answer("❌ Неверная дата. Формат: ГГГГ-ММ-ДД")
        return

    try:
        hour = int(args[1])
    except ValueError:
        await message.answer("❌ Час должен быть числом 0-23")
        return
    if not 0 <= hour <= 23:
        await message.answer("❌ Час должен быть 0-23")
        return

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    master_id, _business_id, _tz = resolved

    # Find slot by (master, date, hour) — close_slot service takes slot_id
    from sqlalchemy import select

    from bot.models import Slot

    async with async_session_factory() as session:
        stmt = select(Slot).where(
            Slot.master_id == master_id,
            Slot.slot_date == slot_date,
            Slot.slot_hour == hour,
        )
        slot = (await session.execute(stmt)).scalar_one_or_none()
        if slot is None:
            await message.answer(f"❌ Слот {slot_date} {hour}:00 не найден")
            return

        try:
            updated = await close_slot(session, slot.id)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return

    if updated:
        await message.answer(f"✅ Слот {slot_date.strftime('%d %B')} {hour}:00 закрыт")
    else:
        await message.answer("Слот уже был закрыт")


# ============================================================
# 3. /today — bookings for today (LOCAL date)
# ============================================================
@router.message(Command("today"), StateFilter(None))
async def cmd_today(message: Message) -> None:
    """List confirmed/transferred bookings for today."""
    admin_id = _require_admin_or_silent(message)
    if admin_id is None:
        return
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_today_bookings(session, master_id, tz)

    if not bookings:
        await message.answer("На сегодня записей нет.")
        return

    await message.answer(_render_bookings("📅 Записи на сегодня:", bookings, tz))


# ============================================================
# 4. /week — bookings for next 7 days
# ============================================================
@router.message(Command("week"), StateFilter(None))
async def cmd_week(message: Message) -> None:
    """List confirmed/transferred bookings for next 7 days (LOCAL today → today+7)."""
    admin_id = _require_admin_or_silent(message)
    if admin_id is None:
        return
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_week_bookings(session, master_id, tz, days_ahead=7)

    if not bookings:
        await message.answer("На ближайшую неделю записей нет.")
        return

    await message.answer(_render_bookings("📅 Записи на неделю:", bookings, tz))


# ============================================================
# 5. /services add <name> <duration_min> <price>
# ============================================================
@router.message(Command("services"), StateFilter(None))
async def cmd_services(message: Message, command: CommandObject) -> None:
    """Add a service: `services add Стрижка 60 1500`.

    Note: name with spaces NOT supported in this MVP (no quotes parsing).
    Use single-word name or replace spaces with underscores: "Стрижка_мужская".
    """
    if not _is_admin(message):
        return

    args = (command.args or "").split()
    if not args or args[0] != "add":
        await message.answer(
            "Формат: <code>/services add НАЗВАНИЕ ДЛИТЕЛЬНОСТЬ_МИН ЦЕНА</code>\n"
            "Пример: <code>/services add Стрижка 60 1500</code>\n"
            "Внимание: название без пробелов (замените на _)"
        )
        return

    if len(args) != 4:
        await message.answer(
            "❌ Нужно 3 параметра: НАЗВАНИЕ ДЛИТЕЛЬНОСТЬ_МИН ЦЕНА\n"
            "Пример: <code>/services add Стрижка 60 1500</code>"
        )
        return

    name = args[1].replace("_", " ")
    try:
        duration = int(args[2])
    except ValueError:
        await message.answer("❌ Длительность должна быть числом минут (например 60)")
        return
    try:
        price = Decimal(args[3])
    except InvalidOperation:
        await message.answer("❌ Цена должна быть числом (например 1500)")
        return

    if duration <= 0:
        await message.answer("❌ Длительность должна быть > 0")
        return
    if price < 0:
        await message.answer("❌ Цена не может быть отрицательной")
        return

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    _master_id, business_id, _tz = resolved

    async with async_session_factory() as session:
        try:
            service = await create_service(session, business_id, name, duration, price)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return

    await message.answer(
        f"✅ Услуга добавлена:\n"
        f"💇 <b>{html.escape(service.name, quote=False)}</b>\n"
        f"⏱ {service.duration_minutes} мин\n"
        f"💰 {service.price} ₽"
    )


# ============================================================
# Render helper — shared by /today and /week
# ============================================================
def _render_bookings(title: str, bookings: list[Booking], business_timezone: str) -> str:
    """Render bookings list. client_name_snapshot + service_title_snapshot
    are already html.escape()'d in DB — no re-escape needed.

    Newline defense: `html.escape(quote=False)` does NOT strip `\n` (verified
    2026-08-21). A multi-line client_name_snapshot would break list formatting.
    We replace `\n` with space here (display-only, DB stays intact).
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(business_timezone)
    lines = [title, ""]
    for b in bookings:
        # b.start_at: naive on SQLite, aware UTC on Postgres. Inject tzinfo=UTC
        # (no-op on Postgres) before .astimezone — Python interprets naive as
        # system-local TZ otherwise.
        local_time = b.start_at.replace(tzinfo=UTC).astimezone(tz)
        when = local_time.strftime("%d %b, %H:%M")
        # Strip newlines from already-escaped snapshots to preserve list layout
        name = b.client_name_snapshot.replace("\n", " ")
        service = b.service_title_snapshot.replace("\n", " ")
        lines.append(f"• {when} — {name}, {service}")
    return "\n".join(lines)


# ============================================================
# Inline menu callbacks (Вариант B, spec.md 251) — Этап 1.3a
#
# 5 точек входа в flow. StateFilter("*") чтобы поймать тап в любом
# state (включая mid-FSM другого flow) — critic UX-edge: тап по меню
# прерывает текущий flow через state.clear() + set_state(new).
#
# Для today/week (read-only, мгновенный список) — state НЕ трогаем
# (админ может посмотреть расписание mid-flow без отмены).
#
# Auth: _is_admin_callback (callback.from_user, не message.from_user).
# Non-admin tap → silent callback.answer() (admin-menu клиенту не виден).
# ============================================================


def _admin_calendar_range(business_timezone: str) -> tuple[datetime, datetime]:
    """(min_date, max_date) для admin SimpleCalendar — naive local midnight.

    min = today_local @ 00:00, max = today_local + 365d @ 00:00.
    Аналог _calendar_range в client.py:77, но использует business.timezone
    из DB (а не settings.TIMEZONE) — MVP single-master совпадают, но
    семантически calendar должен следовать бизнес-таймзоне.
    """
    tz = ZoneInfo(business_timezone)
    today_local = datetime.now(tz).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    max_local = today_local + timedelta(days=365)
    return today_local, max_local


@router.callback_query(AdminAddslotsCallbackData.filter(), StateFilter("*"))
async def admin_addslots_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: открыть слоты — start addslots flow.

    UX-edge: tap mid-FSM → state.clear() + set_state(adding_slots_date).
    Show SimpleCalendar for date selection (next handler — 1.3b).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (mypy не сужает тип
    # после вызова функции — assert здесь как type narrowing, runtime no-op).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    await state.clear()
    await state.set_state(AdminStates.adding_slots_date)

    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите дату для открытия слотов:",
            reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
        )
    await callback.answer()


# ============================================================
# Inline menu callbacks (Вариант B, spec.md 251) — Этап 1.3b
#
# FSM для addslots: date (SimpleCalendar) → hours (text input) → create.
# Calendar handler закрыл W3 (calendar-tap dangling из 1.3a code-review).
#
# INL-001 (donor: UznetDev/Aiogram-Bot-Template/main_admin_panel.py:30-34):
# при переходе calendar → ask hours ИСПОЛЬЗУЕМ edit_message_text (НЕ answer),
# чат админа не засоряется. Fallback на answer если message нельзя edit
# (>48h / удалено — TelegramBadRequest).
# ============================================================


@router.callback_query(SimpleCalendarCallback.filter(), StateFilter(AdminStates.adding_slots_date))
async def admin_addslots_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar для addslots — навигация + выбор даты.

    Branch by callback_data.act (паттерн из client.py:124-211):
    - ignore/today+same-month: callback.answer(cache_time=60) — lib не вызывается
    - day: сохраняем selected_date, set_state(adding_slots_hours), edit_message_text
    - cancel: state.clear() + edit "Отменено"
    - navigation (prev_y/next_y/prev_m/next_m/today-diff-month): lib сделал
      edit_reply_markup, handler answers
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await state.clear()
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    # act=ignore/today+same-month: lib не вызывается (early return), handler answers
    # cache_time=60 — contract с aiogram_calendar 0.6.0 (lib answer на этих ветках
    # сам если бы вызвался, handler делает это вместо).
    if callback_data.act == SimpleCalAct.ignore:
        await callback.answer(cache_time=60)
        return
    if callback_data.act == SimpleCalAct.today:
        # lib использует system-local datetime.now() для same-month check
        # (simple_calendar.py:173) — handler матч'ит чтобы избежать TZ-mismatch.
        today_sys = datetime.now().replace(tzinfo=None)
        if today_sys.year == callback_data.year and today_sys.month == callback_data.month:
            await callback.answer(cache_time=60)
            return  # same-month: lib бы answer cache_time=60, handler вместо

    cal = SimpleCalendar(locale="ru_RU", cancel_btn="Отмена", today_btn="Сегодня")
    cal.set_dates_range(*_admin_calendar_range(tz))
    selected, selected_date = await cal.process_selection(callback, callback_data)

    if callback_data.act == SimpleCalAct.day:
        if not selected:
            return  # out-of-range — lib answered alert, do nothing
        slot_date = selected_date.date()
        await state.update_data(selected_date=slot_date.isoformat())
        await state.set_state(AdminStates.adding_slots_hours)

        # INL-001: edit_message_text — чат не засоряется.
        # reply_markup=None убирает calendar keyboard.
        # isinstance сужает Message | InaccessibleMessage → Message (у
        # InaccessibleMessage нет edit_text, mypy:union-attr).
        ask_text = (
            f"Дата: <b>{slot_date.strftime('%d %B %Y')}</b>\n"
            "Введите часы через пробел (например <code>11 12 13 14</code>):"
        )
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(ask_text, reply_markup=None)
            except TelegramBadRequest:
                # message нельзя edit (>48h, удалено) — fallback на новое сообщение
                await callback.message.answer(ask_text)
        await callback.answer()
        return

    if callback_data.act == SimpleCalAct.cancel:
        await state.clear()
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    "❌ Открытие слотов отменено. /menu — заново",
                    reply_markup=None,
                )
            except TelegramBadRequest:
                await callback.message.answer("❌ Открытие слотов отменено. /menu — заново")
        await callback.answer()
        return

    # Navigation (prev_y/next_y/prev_m/next_m/today-diff-month):
    # lib сделал edit_reply_markup, handler answers.
    await callback.answer()


@router.message(StateFilter(AdminStates.adding_slots_hours), F.text, ~F.text.startswith("/"))
async def admin_addslots_hours_msg(message: Message, state: FSMContext) -> None:
    """User typed hours → parse + add_slots + render result.

    Re-use парсинга из cmd_addslots:148-180. State терминальный — state.clear() в конце.

    NB W1 (code-review 1.3b): `~F.text.startswith("/")` в фильтре — НЕ ловит команды.
    `/cancel` из этого state проваливается в client_router (client.py:443,
    Command("cancel") + StateFilter("*")) — документированный escape hatch.
    Полный catch-all (для non-/ текст) — Этап 1.3e.
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    selected_date_iso = data.get("selected_date")
    if not selected_date_iso:
        # State loss mid-flow (restart, timeout) — graceful exit, ask заново.
        await state.clear()
        await message.answer("❌ Дата не выбрана. Откройте слоты заново через /menu")
        return

    try:
        slot_date = date.fromisoformat(selected_date_iso)
    except ValueError:
        # Защита от протухшего/повреждённого state (defense-in-depth).
        await state.clear()
        await message.answer("❌ Ошибка даты в сессии. Начните заново через /menu")
        return

    # Parse hours (re-use cmd_addslots:148-154 logic).
    text = message.text or ""
    try:
        hours_raw = [int(h) for h in text.split()]
    except ValueError:
        await message.answer("❌ Часы должны быть числами 0-23 (например 11 12 13)")
        return  # state stays — ask again

    if not hours_raw:
        await message.answer("❌ Введите хотя бы один час (например 11 12 13)")
        return  # state stays

    hours = sorted(set(hours_raw))  # dedup (composite UNIQUE в DB)

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        # W2 (code-review 1.3b): clear state — unrecoverable error, consistency
        # с calendar handler (514-517) и past-date check ниже.
        await state.clear()
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, tz = resolved

    # spec.md 401: slot_date must not be in the past (defensive — calendar уже фильтровал,
    # но между выбором даты и вводом часов мог пройти день).
    today_local = datetime.now(ZoneInfo(tz)).date()
    if slot_date < today_local:
        await message.answer(
            f"❌ Нельзя создать слот в прошлом. Сегодня: {today_local.strftime('%d.%m.%Y')}"
        )
        await state.clear()
        return

    async with async_session_factory() as session:
        try:
            created = await add_slots(session, master_id, slot_date, hours)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return  # state stays — может исправить часы
        except SlotAlreadyExistsError:
            await message.answer("❌ Один из слотов уже существует (гонка). Попробуйте ещё раз.")
            return

    await state.clear()  # терминальный
    if created:
        hours_str = ", ".join(str(s.slot_hour) for s in created)
        await message.answer(
            f"✅ Открыты слоты на {slot_date.strftime('%d %B')}:\n<b>{hours_str}</b>"
        )
    else:
        await message.answer(
            f"Все слоты на {slot_date.strftime('%d %B')} уже открыты — ничего не добавлено."
        )


@router.callback_query(AdminCloseslotCallbackData.filter(), StateFilter("*"))
async def admin_closeslot_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: закрыть слот — start closeslot flow.

    UX-edge: tap mid-FSM → state.clear() + set_state(closing_slot_date).
    Show SimpleCalendar for date selection (next handler — 1.3c).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    await state.clear()
    await state.set_state(AdminStates.closing_slot_date)

    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите дату слота для закрытия:",
            reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
        )
    await callback.answer()


# ============================================================
# FSM closeslot (Вариант B, spec.md 251) — Этап 1.3c
#
# Адаптация паттерна 1.3b (addslots), отличия:
# - state: closing_slot_date → closing_slot_hour (НЕ adding_slots_*)
# - hour input: SINGLE час (не список через пробел)
# - service: close_slot (НЕ add_slots) — нужен find slot по (master, date, hour)
# - render: "✅ Слот {date} {hour}:00 закрыт" (НЕ список часов)
# - past-date re-check ОТСУТСТВУЕТ (отличие от 1.3b) — закрыть slot в прошлом
#   валидно (отменить невыполненную запись). cmd_closeslot тоже не проверяет.
#   Calendar min=today всё равно не даёт выбрать прошлое.
# - INL-001: edit_message_text при calendar → ask hour (fallback на answer).
# - W1 analog: ~F.text.startswith("/") в фильтре hour_msg → /cancel проваливается
#   в client_router (escape hatch).
# - W2 analog: state.clear() в branch "Мастер не найден" в hour_msg.
# ============================================================


@router.callback_query(SimpleCalendarCallback.filter(), StateFilter(AdminStates.closing_slot_date))
async def admin_closeslot_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar для closeslot — навигация + выбор даты.

    Branch by callback_data.act (копия admin_addslots_calendar_cb, отличия:
    state closing_slot_* вместо adding_slots_*, текст "Введите час (один, 0-23):"
    вместо "Введите часы через пробел", "Закрытие слота отменено" вместо
    "Открытие слотов отменено").
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await state.clear()
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    # act=ignore/today+same-month: lib не вызывается (early return), handler answers
    if callback_data.act == SimpleCalAct.ignore:
        await callback.answer(cache_time=60)
        return
    if callback_data.act == SimpleCalAct.today:
        today_sys = datetime.now().replace(tzinfo=None)
        if today_sys.year == callback_data.year and today_sys.month == callback_data.month:
            await callback.answer(cache_time=60)
            return

    cal = SimpleCalendar(locale="ru_RU", cancel_btn="Отмена", today_btn="Сегодня")
    cal.set_dates_range(*_admin_calendar_range(tz))
    selected, selected_date = await cal.process_selection(callback, callback_data)

    if callback_data.act == SimpleCalAct.day:
        if not selected:
            return  # out-of-range — lib answered alert
        slot_date = selected_date.date()
        await state.update_data(selected_date=slot_date.isoformat())
        await state.set_state(AdminStates.closing_slot_hour)

        # INL-001: edit_message_text — чат не засоряется.
        ask_text = (
            f"Дата: <b>{slot_date.strftime('%d %B %Y')}</b>\n"
            "Введите час (один, 0-23):"
        )
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(ask_text, reply_markup=None)
            except TelegramBadRequest:
                await callback.message.answer(ask_text)
        await callback.answer()
        return

    if callback_data.act == SimpleCalAct.cancel:
        await state.clear()
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    "❌ Закрытие слота отменено. /menu — заново",
                    reply_markup=None,
                )
            except TelegramBadRequest:
                await callback.message.answer("❌ Закрытие слота отменено. /menu — заново")
        await callback.answer()
        return

    # Navigation (prev_y/next_y/prev_m/next_m/today-diff-month):
    # lib сделал edit_reply_markup, handler answers.
    await callback.answer()


@router.message(StateFilter(AdminStates.closing_slot_hour), F.text, ~F.text.startswith("/"))
async def admin_closeslot_hour_msg(message: Message, state: FSMContext) -> None:
    """User typed hour → parse + find slot + close_slot + render result.

    Re-use cmd_closeslot:218-267 logic (parse hour, resolve master, find slot
    by composite key, close_slot service). State терминальный — state.clear()
    в success / already-closed / unrecoverable error ветках. State stays в
    "slot not found" / "slot booked" / parse-error (админ может поправить ввод).

    NB W1 (code-review 1.3b): `~F.text.startswith("/")` в фильтре — НЕ ловит
    команды. `/cancel` из этого state проваливается в client_router (escape
    hatch). Полный catch-all — Этап 1.3e.
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    selected_date_iso = data.get("selected_date")
    if not selected_date_iso:
        # State loss mid-flow (restart, timeout) — graceful exit, ask заново.
        await state.clear()
        await message.answer("❌ Дата не выбрана. Закройте слот заново через /menu")
        return

    try:
        slot_date = date.fromisoformat(selected_date_iso)
    except ValueError:
        # Защита от протухшего/повреждённого state (defense-in-depth).
        await state.clear()
        await message.answer("❌ Ошибка даты в сессии. Начните заново через /menu")
        return

    # Parse SINGLE hour (отличие от 1.3b — int, не list[int] из text.split()).
    text = (message.text or "").strip()
    try:
        hour = int(text)
    except ValueError:
        await message.answer("❌ Час должен быть числом 0-23 (например 14)")
        return  # state stays — ask again
    if not 0 <= hour <= 23:
        await message.answer("❌ Час должен быть 0-23")
        return  # state stays

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        # W2 analog (code-review 1.3b): clear state — unrecoverable error,
        # consistency с calendar handler выше.
        await state.clear()
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, _tz = resolved

    # NB: past-date re-check ОТСУТСТВУЕТ (отличие от 1.3b:635-643). Закрыть slot
    # в прошлом валидно (отменить невыполненную запись). cmd_closeslot тоже не
    # проверяет. Calendar min=today не даёт выбрать прошлое через UI, но если
    # админ ввёл дату вручную через /closeslot — past валиден.

    # Find slot by (master, date, hour) — re-use cmd_closeslot:243-253 logic.
    from sqlalchemy import select

    from bot.models import Slot

    async with async_session_factory() as session:
        stmt = select(Slot).where(
            Slot.master_id == master_id,
            Slot.slot_date == slot_date,
            Slot.slot_hour == hour,
        )
        slot = (await session.execute(stmt)).scalar_one_or_none()
        if slot is None:
            # State stays — админ мог опечататься в часе, попробовать другой.
            await message.answer(f"❌ Слот {slot_date.strftime('%d %B')} {hour}:00 не найден")
            return

        try:
            updated = await close_slot(session, slot.id)
        except ValueError as exc:
            # Slot booked — close_slot raises ValueError. State stays — админ
            # может попробовать другой час (этот slot не закрывается).
            await message.answer(f"❌ {exc}")
            return

    await state.clear()  # терминальный
    if updated:
        await message.answer(f"✅ Слот {slot_date.strftime('%d %B')} {hour}:00 закрыт")
    else:
        # close_slot вернул False — slot не найден (гонка: удалён между
        # SELECT и UPDATE). Редкий кейс, считаем терминальным.
        await message.answer(f"❌ Слот {slot_date.strftime('%d %B')} {hour}:00 не найден")


@router.callback_query(AdminTodayCallbackData.filter(), StateFilter("*"))
async def admin_today_cb(callback: CallbackQuery) -> None:
    """Menu tap: сегодня — мгновенный список записей (no FSM).

    State НЕ трогаем — админ может посмотреть расписание mid-flow без отмены.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_today_bookings(session, master_id, tz)

    if callback.message is not None:
        if not bookings:
            await callback.message.answer("На сегодня записей нет.")
        else:
            await callback.message.answer(_render_bookings("📅 Записи на сегодня:", bookings, tz))
    await callback.answer()


@router.callback_query(AdminWeekCallbackData.filter(), StateFilter("*"))
async def admin_week_cb(callback: CallbackQuery) -> None:
    """Menu tap: неделя — мгновенный список записей на 7 дней (no FSM).

    State НЕ трогаем — админ может посмотреть расписание mid-flow без отмены.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_week_bookings(session, master_id, tz, days_ahead=7)

    if callback.message is not None:
        if not bookings:
            await callback.message.answer("На ближайшую неделю записей нет.")
        else:
            await callback.message.answer(_render_bookings("📅 Записи на неделю:", bookings, tz))
    await callback.answer()


@router.callback_query(AdminServicesCallbackData.filter(), StateFilter("*"))
async def admin_services_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: добавить услугу — start entering_service flow (3 шага).

    UX-edge: tap mid-FSM → state.clear() + set_state(entering_service_name).
    Next handlers (1.3d) ask for name → duration → price.

    NB W1 (code-review 1.3a): resolve master+business на entry (НЕ в последнем
    handler price_msg) — business_id нужен для create_service в конце flow.
    Сохраняем в state, чтобы price_msg не делал повторный DB lookup.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, business_id, _tz = resolved

    await state.clear()
    await state.update_data(business_id=str(business_id))
    await state.set_state(AdminStates.entering_service_name)

    if callback.message is not None:
        await callback.message.answer("💇 Введите название услуги (можно с пробелами):")
    await callback.answer()


# ============================================================
# FSM services (Вариант B, spec.md 251) — Этап 1.3d
#
# 3 шага: name → duration → price → create_service.
# Re-use cmd_services:349-388 logic (parse name/duration/price, validate,
# create_service). State terminal в price_msg success — state.clear().
#
# NB INL-001 НЕ ПРИМЕНИМ для 1.3d: edit_message_text работает только для
# callback→text переходов (calendar→ask hour в 1.3b/c), где edit убирает
# календарь. В 1.3d между шагами — message от юзера + новое message от бота:
# edit_message_text нечего редактировать (сообщение юзера нельзя править
# ботом, предыдущее сообщение бота уже показано). Используем answer (как в
# cmd_services) — чат засоряется, но для MVP acceptable.
#
# NB name: НЕ делаем replace("_", " ") (отличие от cmd_services:349). В 1.3d
# юзер вводит name целиком через message.text, как написано так и сохраняем.
# cmd_services делал replace из-за arg-splitting в одной строке ("/services
# add Стрижка_мужская 60 1500" — args[1] = "Стрижка_мужская").
#
# W1 analog: ~F.text.startswith("/") в фильтре всех 3 msg handlers → /cancel
# проваливается в client_router (escape hatch).
# W2 analog: state.clear() в unrecoverable error ветках (state loss, master
# not found).
# ============================================================


@router.message(StateFilter(AdminStates.entering_service_name), F.text, ~F.text.startswith("/"))
async def admin_service_name_msg(message: Message, state: FSMContext) -> None:
    """User typed service name → save + ask duration.

    State stays в empty-name (ask again). set_state(entering_service_duration)
    в success. /cancel через client_router escape hatch (W1 analog).
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    business_id_str = data.get("business_id")
    if not business_id_str:
        # State loss mid-flow (restart, timeout) — graceful exit, ask заново.
        await state.clear()
        await message.answer("❌ Сессия утеряна. Добавьте услугу заново через /menu")
        return

    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Введите название:")
        return  # state stays — ask again
    if len(name) > 255:
        # S1 (code-review 1.3d): early validation — не ждать price_msg чтобы
        # узнать про name>255 (create_service бы поднял ValueError, но после
        # 3 впустую потраченных шагов). Лучше early fail.
        await message.answer("❌ Слишком длинное название (макс 255 символов). Введите короче:")
        return  # state stays

    await state.update_data(name=name)
    await state.set_state(AdminStates.entering_service_duration)
    await message.answer(
        f"Название: <b>{html.escape(name, quote=False)}</b>\n"
        "Введите длительность (мин, число > 0):"
    )


@router.message(StateFilter(AdminStates.entering_service_duration), F.text, ~F.text.startswith("/"))
async def admin_service_duration_msg(message: Message, state: FSMContext) -> None:
    """User typed duration → parse + save + ask price.

    State stays в parse-error / <=0 (ask again). set_state(entering_service_price)
    в success. /cancel через client_router escape hatch (W1 analog).
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    # Defense-in-depth: проверяем что name и business_id ещё живы (state loss
    # между шагами name→duration).
    if not data.get("name") or not data.get("business_id"):
        await state.clear()
        await message.answer("❌ Сессия утеряна. Добавьте услугу заново через /menu")
        return

    text = (message.text or "").strip()
    try:
        duration = int(text)
    except ValueError:
        await message.answer("❌ Длительность должна быть числом минут (например 60)")
        return  # state stays
    if duration <= 0:
        await message.answer("❌ Длительность должна быть > 0")
        return  # state stays

    await state.update_data(duration=duration)
    await state.set_state(AdminStates.entering_service_price)
    await message.answer(
        f"Длительность: <b>{duration} мин</b>\n"
        "Введите цену (₽, число, можно с копейками):"
    )


@router.message(StateFilter(AdminStates.entering_service_price), F.text, ~F.text.startswith("/"))
async def admin_service_price_msg(message: Message, state: FSMContext) -> None:
    """User typed price → parse + validate + create_service + render result.

    Re-use cmd_services:356-388 logic (Decimal parse, validate >=0,
    create_service, render). State terminal — state.clear() в success и
    unrecoverable error. State stays в parse-error / ValueError от service
    (админ может поправить ввод цены, retryable).

    /cancel через client_router escape hatch (W1 analog).
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    name = data.get("name")
    business_id_str = data.get("business_id")
    duration = data.get("duration")
    if not name or not business_id_str or duration is None:
        # State loss mid-flow (restart, timeout) — graceful exit.
        await state.clear()
        await message.answer("❌ Сессия утеряна. Добавьте услугу заново через /menu")
        return

    text = (message.text or "").strip()
    try:
        price = Decimal(text)
    except InvalidOperation:
        await message.answer("❌ Цена должна быть числом (например 1500 или 1500.50)")
        return  # state stays
    if price < 0:
        await message.answer("❌ Цена не может быть отрицательной")
        return  # state stays
    # F1 fix (code-review 1.3d): Decimal inf/nan/overflow ловим ДО create_service.
    # Decimal("inf")/Decimal("nan") парсятся успешно, проходят price<0 check,
    # но Postgres NUMERIC(10,2) не поддерживает inf/nan → DataError (НЕ
    # ValueError) → except ValueError ниже не ловит → state hang. is_finite()
    # возвращает False для inf/-inf/nan — единая проверка.
    if not price.is_finite():
        await message.answer("❌ Цена должна быть конечным числом (не inf/nan)")
        return  # state stays
    # NUMERIC(10, 2) max = 99999999.99 (models.py:79). 1e10 = overflow.
    if price > Decimal("99999999.99"):
        await message.answer("❌ Цена слишком большая (макс 99999999.99)")
        return  # state stays

    try:
        business_id = UUID(business_id_str)
    except ValueError:
        # Corrupted state (business_id not valid UUID) — defense-in-depth.
        await state.clear()
        await message.answer("❌ Ошибка сессии (business_id). Начните заново через /menu")
        return

    # W1 fix (code-review 1.3d): int(duration) cast из Any state dict может
    # поднять ValueError/TypeError если storage повреждён. Оборачиваем.
    try:
        duration_int = int(duration)
    except (ValueError, TypeError):
        await state.clear()
        await message.answer("❌ Ошибка сессии (duration). Начните заново через /menu")
        return

    async with async_session_factory() as session:
        try:
            service = await create_service(session, business_id, name, duration_int, price)
        except ValueError as exc:
            # create_service defense-in-depth (name >255 — уже в name_msg,
            # duration<=0 — duration_msg, price<0 — здесь выше). Если service
            # всё же поднимает ValueError на edge case — state stays, retryable.
            await message.answer(f"❌ {exc}")
            return  # state stays
        except SQLAlchemyError:
            # W2 fix (code-review 1.3d): DB-level ошибки (IntegrityError на
            # constraint, DataError на invalid value, OperationalError на
            # connection loss) — НЕ ValueError, не ловились except выше.
            # State hang — unrecoverable, очищаем.
            await state.clear()
            await message.answer("❌ Ошибка БД. Начните заново через /menu")
            return

    await state.clear()  # терминальный
    await message.answer(
        f"✅ Услуга добавлена:\n"
        f"💇 <b>{html.escape(service.name, quote=False)}</b>\n"
        f"⏱ {service.duration_minutes} мин\n"
        f"💰 {service.price} ₽"
    )
