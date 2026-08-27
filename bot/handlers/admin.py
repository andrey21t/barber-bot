"""Master/admin commands + inline-menu callbacks — Pure I/O layer.

Contract (spec.md 200-213, 251, 307-309):
- Auth: F.from_user.id == settings.ADMIN_ID (MVP single-master). TODO Ур. 2.6:
  extract to middleware/role.py (spec 258) — DB lookup current_business, current_master.
  _is_admin — для Message, _is_admin_callback — для CallbackQuery (callback.from_user).
- Stateful: PARTIAL — command handlers (/addslots /closeslot /today /week /services)
  run StateFilter(None) (no FSM). Inline-menu callbacks (Этап 1.3a) — StateFilter("*"),
  стартуют FSM-потоки через state.set_state(AdminStates.*).
- 5 command handlers + 5 inline-menu callbacks (alias точки входа в flow).
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

from aiogram import Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

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

    # _is_admin_callback guarantees callback.from_user is not None.
    user_id = callback.from_user.id if callback.from_user is not None else 0
    resolved = await _resolve_master_and_business(user_id)
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


@router.callback_query(AdminCloseslotCallbackData.filter(), StateFilter("*"))
async def admin_closeslot_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: закрыть слот — start closeslot flow.

    UX-edge: tap mid-FSM → state.clear() + set_state(closing_slot_date).
    Show SimpleCalendar for date selection (next handler — 1.3c).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None.
    user_id = callback.from_user.id if callback.from_user is not None else 0
    resolved = await _resolve_master_and_business(user_id)
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


@router.callback_query(AdminTodayCallbackData.filter(), StateFilter("*"))
async def admin_today_cb(callback: CallbackQuery) -> None:
    """Menu tap: сегодня — мгновенный список записей (no FSM).

    State НЕ трогаем — админ может посмотреть расписание mid-flow без отмены.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None.
    user_id = callback.from_user.id if callback.from_user is not None else 0
    resolved = await _resolve_master_and_business(user_id)
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

    # _is_admin_callback guarantees callback.from_user is not None.
    user_id = callback.from_user.id if callback.from_user is not None else 0
    resolved = await _resolve_master_and_business(user_id)
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
    Next handler (1.3d) asks for name → duration → price.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    await state.clear()
    await state.set_state(AdminStates.entering_service_name)

    if callback.message is not None:
        await callback.message.answer("💇 Введите название услуги (можно с пробелами):")
    await callback.answer()
