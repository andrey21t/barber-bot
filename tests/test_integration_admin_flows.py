"""Integration-level smoke tests — full Dispatcher + real router wiring.

Captures bugs that pure-unit (handler-direct-invocation) tests miss:
- StateFilter mismatch (e.g. /menu StateFilter(None) was unreachable mid-FSM,
  discovered in Session 5.26 production — unit tests passed because they set
  state directly, not through router dispatch)
- CallbackData prefix collisions
- Router registration order (catchall router must be last)
- state.set_state transitions across multi-step flows

Approach: build a real Dispatcher (admin_router + start_router + client_router)
with MemoryStorage, feed real Telegram Update objects via dp.feed_update(bot, update),
assert bot.send_message / message.answer calls. MockedBot from aiogram_tests
would be cleaner but introduces a dependency — we use AsyncMock + manual wire
to stay dep-free (mirror conftest.py:4 "aiogram.tests НЕ существует").

Coverage (Session 5.27):
- /menu from fresh state → menu shown
- /menu from mid-FSM state → state cleared + menu shown (regression for
  StateFilter(None) bug fixed in 2b9e5bb)
- /openweek full flow: cmd → entry callback → start picker tap → end picker
  tap → days keyboard → weekday toggle → confirm → summary with ✅/❌
- /openweek state escape: mid-flow /menu clears state and shows menu
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from sqlalchemy import select

# Env BEFORE bot imports — mirror conftest.py
os.environ.setdefault("BOT_TOKEN", "test:TOKEN")
os.environ.setdefault("ADMIN_ID", "461355056")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./barber.db")

from bot.config import get_settings  # noqa: E402
from bot.handlers.admin import router as admin_router  # noqa: E402
from bot.handlers.client import router as client_router  # noqa: E402
from bot.handlers.start import router as start_router  # noqa: E402
from bot.models import Business, Client, Master, WorkDay  # noqa: E402

ADMIN_TG_ID = get_settings().ADMIN_ID
TZ = "Europe/Moscow"


# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture
async def integration_dispatcher(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Dispatcher, RecordingBot]:
    """Real Dispatcher with admin_router+start_router+client_router wired,
    MemoryStorage for FSM, RecordingBot that records bot(method) calls.

    Patches `bot.handlers.admin.async_session_factory` so handler DB calls
    hit in-memory SQLite (same approach as patched_session_factory fixture).

    Routers are module-level singletons — aiogram refuses to attach the
    same Router to a second Dispatcher. We detach from any previous parent
    before re-attaching (works across tests within same session).
    """
    monkeypatch.setattr("bot.handlers.admin.async_session_factory", session_factory)
    monkeypatch.setattr("bot.handlers.client.async_session_factory", session_factory)

    for r in (start_router, admin_router, client_router):
        if r._parent_router is not None:  # noqa: SLF001 — aiogram internal
            r._parent_router.sub_routers.remove(r)  # noqa: SLF001
            r._parent_router = None  # noqa: SLF001

    dp = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
    )
    dp["scheduler"] = MagicMock()  # AsyncIOScheduler stub
    dp.include_router(start_router)
    dp.include_router(admin_router)
    dp.include_router(client_router)

    bot = RecordingBot()

    return dp, bot


class RecordingBot:
    """Mock Bot that records aiogram method calls. aiogram Message.answer()
    calls `await bot(SendMessage(...))` (not bot.send_message directly). We
    record the SendMessage/EditMessageText/etc objects passed to __call__.

    Returns a stub Message (id=1, chat=id, text from SendMessage.text) so
    chained `await message.answer(...)` works inside handlers.
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []  # list of aiogram method objects
        self.id = 1
        self.username = "test_bot"

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        from aiogram.types import Chat
        from aiogram.types import Message as AioMessage

        chat_id = getattr(method, "chat_id", None) or 1
        text = getattr(method, "text", None) or ""
        return AioMessage(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )

    @property
    def last_call(self) -> Any:
        return self.calls[-1] if self.calls else None

    @property
    def last_text(self) -> str:
        # Iterate in reverse — return text of last SendMessage (skip
        # AnswerCallbackQuery/EditMessageText which are noisy trailing calls).
        # Most flows end with `await callback.answer()` after SendMessage, so
        # naive last_call would shadow the actual sent message text.
        from aiogram.methods import EditMessageText, SendMessage

        for c in reversed(self.calls):
            if isinstance(c, SendMessage | EditMessageText):
                return getattr(c, "text", "") or ""
        return ""

    @property
    def last_reply_markup(self) -> Any:
        # Same filtering as last_text — last SendMessage/EditMessageText only.
        from aiogram.methods import EditMessageText, SendMessage

        for c in reversed(self.calls):
            if isinstance(c, SendMessage | EditMessageText):
                return getattr(c, "reply_markup", None)
        return None

    def reset(self) -> None:
        self.calls.clear()

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        """Stub for callback.bot.send_message (used by confirm_cb to notify
        master). Returns a stub Message — handler doesn't await on it.
        """
        from aiogram.types import Chat
        from aiogram.types import Message as AioMessage

        chat_id = kwargs.get("chat_id") or (args[0] if args else 1)
        text = kwargs.get("text", "")
        return AioMessage(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )


async def _seed_admin(
    session_factory: Any,
    *,
    admin_id: int = ADMIN_TG_ID,
    timezone: str = TZ,
) -> dict[str, Any]:
    async with session_factory() as session:
        biz = Business(name="Test", telegram_owner_id=admin_id, timezone=timezone)
        session.add(biz)
        await session.flush()
        master = Master(business_id=biz.id, name="T", telegram_id=admin_id, role="owner")
        session.add(master)
        await session.flush()
        client = Client(telegram_id=999888777, name="Client")
        session.add(client)
        await session.commit()
        return {
            "business_id": biz.id,
            "master_id": master.id,
            "client_id": client.id,
            "master_telegram_id": admin_id,
            "client_telegram_id": 999888777,
        }


def _make_text_update(text: str, user_id: int = ADMIN_TG_ID, chat_id: int = ADMIN_TG_ID) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            text=text,
        ),
    )


def _extract_send_text(bot: Any) -> str:
    """Extract text from the LAST SendMessage call (integration tests
    typically assert on the final message after multi-step flow).
    """
    text: str = bot.last_text
    return text


def _extract_reply_markup(bot: Any) -> Any:
    return bot.last_reply_markup


def _make_callback_update_from_button(
    button: InlineKeyboardButton,
    *,
    user_id: int = ADMIN_TG_ID,
    chat_id: int = ADMIN_TG_ID,
    message_id: int = 1,
) -> Update:
    """Build Update from an inline-button of a previously-sent message.
    Mirrors real Telegram: user taps button, bot receives CallbackQuery with
    same callback_data as was in button.callback_data.
    """
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1",
            chat_instance=str(chat_id),
            data=button.callback_data,
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            message=Message(
                message_id=message_id,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text="previous",
            ),
        ),
    )


async def _find_button_by_label(
    bot: MagicMock,
    label: str,
) -> InlineKeyboardButton | None:
    """Find an InlineKeyboardButton with text matching `label` in the LAST
    sent message's reply_markup. Returns None if not found.
    """
    markup = _extract_reply_markup(bot)
    if markup is None or not isinstance(markup, InlineKeyboardMarkup):
        return None
    for row in markup.inline_keyboard:
        for btn in row:
            if label in btn.text:
                return btn
    return None


# ============================================================
# /menu — escape hatch regression
# ============================================================


@pytest.mark.asyncio
async def test_menu_from_fresh_state_shows_menu(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """/menu from fresh state → bot sends '📋 Меню:' with admin_inline_menu.
    Regression for Session 5.26 prod-bug: StateFilter(None) made /menu
    unreachable mid-FSM. StateFilter('*') fix (2b9e5bb) catches via dispatcher.
    """
    dp, bot = integration_dispatcher
    await _seed_admin(session_factory)

    await dp.feed_update(bot, _make_text_update("/menu"))

    text = _extract_send_text(bot)
    assert "Меню" in text


@pytest.mark.asyncio
async def test_menu_escape_from_openweek_mid_flow(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """/menu mid-FlowFSM (after /openweek set state to opening_week_start)
    → state cleared + menu shown. This is the regression that caught the
    StateFilter(None) bug in production — dispatcher would silently skip
    /menu if user is in opening_week_start state.
    """
    dp, bot = integration_dispatcher
    await _seed_admin(session_factory)

    # Step 1: /openweek → sets FSM state to opening_week_start.
    await dp.feed_update(bot, _make_text_update("/openweek"))
    assert "Шаг 1" in _extract_send_text(bot)

    # Reset bot mocks to capture /menu response only.
    bot.reset()

    # Step 2: /menu while FSM is in opening_week_start — must escape.
    await dp.feed_update(bot, _make_text_update("/menu"))

    text = _extract_send_text(bot)
    assert "Меню" in text
    # Verify state was cleared: a subsequent /openweek re-enters cleanly.
    bot.reset()
    await dp.feed_update(bot, _make_text_update("/openweek"))
    assert "Шаг 1" in _extract_send_text(bot)


# ============================================================
# /openweek — full flow smoke
# ============================================================


@pytest.mark.asyncio
async def test_openweek_full_flow_creates_workday(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """End-to-end /openweek flow: cmd → start picker tap → end picker tap →
    weekday Mon toggle → ✅ confirm → summary with ✅. Verifies:
    - All 4 handlers dispatch correctly by StateFilter
    - CallbackData unpacked correctly from inline button
    - state.set_state transitions: start → end → days → (clear on confirm)
    - open_workday called, WorkDay persisted

    Uses future Tuesday as frozen date so Mon (selected) is past → skipped.
    Wait — we want ✅, so we select Wed (future on Tuesday).
    """
    from freezegun import freeze_time

    # Tuesday UTC 14:00 → Moscow 17:00 Tue 25 Aug 2026.
    # Monday=24 (past), Tuesday=25 (today), Wed=27, Thu=28, Fri=29 (future).
    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        admin_ctx = await _seed_admin(session_factory)

        # Step 1: /openweek → start picker.
        await dp.feed_update(bot, _make_text_update("/openweek"))
        step1_text = _extract_send_text(bot)
        assert "Шаг 1" in step1_text

        # Step 2: tap 10:00 in start picker → end picker (Шаг 2).
        start_button = await _find_button_by_label(bot, "10:00")
        assert start_button is not None, "Expected '10:00' button in start picker"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(start_button))
        step2_text = _extract_send_text(bot)
        assert "Шаг 2" in step2_text

        # Step 3: tap 12:00 in end picker → days keyboard (Шаг 3).
        end_button = await _find_button_by_label(bot, "12:00")
        assert end_button is not None, "Expected '12:00' button in end picker"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(end_button))
        step3_text = _extract_send_text(bot)
        assert "Шаг 3" in step3_text

        # Step 4: tap "Ср" weekday toggle button → keyboard re-renders (no new
        # message — handler calls edit_reply_markup, not send_message). We
        # can't easily inspect edit_reply_markup here, but we can verify state
        # by proceeding to confirm.
        wed_button = await _find_button_by_label(bot, "Ср")
        assert wed_button is not None, "Expected 'Ср' weekday toggle button"
        # edit_reply_markup is on the message Mock — invoke via dispatcher
        # but ignore the response (no new message expected).
        await dp.feed_update(bot, _make_callback_update_from_button(wed_button))

        # Step 5: tap "✅ Открыть" confirm button → summary with ✅ Ср.
        confirm_button = await _find_button_by_label(bot, "✅ Открыть")
        # NOTE: confirm_button lookup searches the LAST sent markup (Шаг 3).
        # But /openweek_days_cb does edit_reply_markup — message stays the
        # same, so _extract_reply_markup still returns the days keyboard
        # from Шаг 3 (with toggled ✅ Ср state, but original buttons list
        # unchanged in our mock since edit_reply_markup is AsyncMock).
        # We need the confirm button from the ORIGINAL days keyboard — works.
        assert confirm_button is not None, "Expected '✅ Открыть' confirm button"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(confirm_button))

        summary = _extract_send_text(bot)
        assert "✅" in summary, f"Expected ✅ in summary, got: {summary!r}"
        assert "Ср" in summary, f"Expected 'Ср' in summary, got: {summary!r}"

        # Verify WorkDay created for Wed of frozen week.
        today_local = datetime.now(ZoneInfo(TZ)).date()
        monday = today_local - timedelta(days=today_local.weekday())
        wed_date = monday + timedelta(days=2)
        async with session_factory() as session:
            wd = await session.scalar(
                select(WorkDay).where(
                    WorkDay.master_id == admin_ctx["master_id"],
                    WorkDay.work_date == wed_date,
                )
            )
        assert wd is not None, "WorkDay for Wed not created"
        assert wd.is_active is True
        assert str(wd.start_time) == "10:00:00"
        assert str(wd.end_time) == "12:00:00"


@pytest.mark.asyncio
async def test_openweek_cancel_clears_state(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """/openweek → start → end → days → [❌ Отмена] → state cleared +
    'Открытие недели отменено' message + menu shown.

    Verifies admin_openweek_cancel_cb dispatch on F.data == 'admin_openweek_cancel'
    with StateFilter(opening_week_days). The '❌ Отмена' button only appears
    on the Шаг 3 days keyboard (admin_week_days_keyboard) — slot pickers
    (Шаг 1, Шаг 2) intentionally omit cancel (user uses /menu escape).
    """
    dp, bot = integration_dispatcher
    await _seed_admin(session_factory)

    # Шаг 1: /openweek → start picker.
    await dp.feed_update(bot, _make_text_update("/openweek"))
    assert "Шаг 1" in _extract_send_text(bot)
    start_btn = await _find_button_by_label(bot, "10:00")
    assert start_btn is not None, "10:00 button on Шаг 1 picker"

    # Шаг 2: tap 10:00 → end picker.
    bot.reset()
    await dp.feed_update(bot, _make_callback_update_from_button(start_btn))
    assert "Шаг 2" in _extract_send_text(bot)
    end_btn = await _find_button_by_label(bot, "12:00")
    assert end_btn is not None, "12:00 button on Шаг 2 picker"

    # Шаг 3: tap 12:00 → days keyboard (with [❌ Отмена]).
    bot.reset()
    await dp.feed_update(bot, _make_callback_update_from_button(end_btn))
    assert "Шаг 3" in _extract_send_text(bot)
    cancel_btn = await _find_button_by_label(bot, "❌ Отмена")
    assert cancel_btn is not None, "❌ Отмена button on Шаг 3 days keyboard"

    # Tap [❌ Отмена] → 'Открытие недели отменено' + menu.
    bot.reset()
    await dp.feed_update(bot, _make_callback_update_from_button(cancel_btn))
    text = _extract_send_text(bot)
    assert "Открытие недели отменено" in text

    # State cleared — fresh /openweek re-enters cleanly.
    bot.reset()
    await dp.feed_update(bot, _make_text_update("/openweek"))
    assert "Шаг 1" in _extract_send_text(bot)


# ============================================================
# Session 5.27 FEAT — booking flow E2E with service picker
# Coverage: /slots → calendar → slot → name → service picker → ✅ → booking
# ============================================================


async def _seed_workday_tomorrow(
    session_factory: Any,
    *,
    start_time_str: str = "10:00",
    end_time_str: str = "12:00",
) -> dict[str, Any]:
    """Seed admin + business + master + workday (tomorrow, 10-12 LOCAL).

    Returns dict with master_id, business_id, workday_id, client_telegram_id.
    Client is NOT seeded here — created via /slots booking flow by telegram_id.
    """
    from datetime import time as dt_time

    from bot.models import Service

    async with session_factory() as session:
        biz = Business(name="Test", telegram_owner_id=ADMIN_TG_ID, timezone=TZ)
        session.add(biz)
        await session.flush()
        master = Master(business_id=biz.id, name="T", telegram_id=ADMIN_TG_ID, role="owner")
        session.add(master)
        await session.flush()

        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        wd = WorkDay(
            master_id=master.id,
            work_date=tomorrow,
            start_time=dt_time.fromisoformat(start_time_str),
            end_time=dt_time.fromisoformat(end_time_str),
            is_active=True,
            max_concurrent_clients=1,
        )
        session.add(wd)
        await session.flush()

        # Two active services — picker should show both
        svc1 = Service(business_id=biz.id, name="Стрижка", duration_minutes=60)
        svc2 = Service(business_id=biz.id, name="Окрашивание", duration_minutes=120)
        session.add_all([svc1, svc2])
        await session.commit()

        return {
            "business_id": biz.id,
            "master_id": master.id,
            "workday_id": wd.id,
            "service1_id": svc1.id,
            "service2_id": svc2.id,
        }


def _make_calendar_day_update(
    target_date: Any,
    *,
    user_id: int = ADMIN_TG_ID,
    chat_id: int = ADMIN_TG_ID,
) -> Update:
    """Build Update for SimpleCalendar day-tap (act=DAY, year/month/day set).

    Uses client telegram_id (999_888_777 — same as _seed_admin), not admin.
    """
    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=target_date.year,
        month=target_date.month,
        day=target_date.day,
    ).pack()
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1",
            chat_instance=str(chat_id),
            data=cb_data,
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text="calendar",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_booking_flow_with_service_picker_creates_booking(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 5.27 FEAT E2E: /slots → calendar → slot → name → service
    picker (tap service) → ✅ → booking created with service_id set.

    Verifies:
    - All handlers dispatch correctly (cmd_slots → simple_calendar_cb →
      slot_30_cb → name_msg → service_picker_cb → confirm_cb)
    - service_picker_keyboard shown with both seeded services + 'Своя услуга'
    - Tap service → summary with service name → ✅ → BookingCreate.service_id
      is the UUID (not None) — _build_end_at uses service.duration_minutes
    - Booking persisted to DB with correct service_id, service_title_snapshot,
      end_at = start_at + service.duration_minutes
    """
    from freezegun import freeze_time

    # Freeze on a date where tomorrow is in the future (calendar always
    # allows today + MAX_BOOKING_DAYS_AHEAD). Use a fixed date for determinism.
    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        ctx = await _seed_workday_tomorrow(session_factory)  # workday for 2026-08-26

        # Use a distinct client telegram_id (not admin).
        client_tg = 999_888_777

        # Step 1: /slots → SimpleCalendar shown (selecting_date state).
        await dp.feed_update(bot, _make_text_update("/slots", user_id=client_tg))
        step1 = _extract_send_text(bot)
        assert "Выберите дату" in step1, f"Expected date picker, got: {step1!r}"

        # Step 2: tap tomorrow (2026-08-26) in calendar → slot picker.
        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(
            bot, _make_calendar_day_update(tomorrow, user_id=client_tg)
        )
        step2 = _extract_send_text(bot)
        # Either slot picker shown ('10:00' button) OR 'нет слотов' if workday
        # window is empty. We seeded 10:00-12:00, so picker should appear.
        slot_btn = await _find_button_by_label(bot, "10:00")
        assert slot_btn is not None, (
            f"Expected 10:00 slot button after calendar tap. Got text: {step2!r}"
        )

        # Step 3: tap 10:00 slot → 'На чьё имя?' prompt (entering_name).
        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(slot_btn, user_id=client_tg)
        )
        step3 = _extract_send_text(bot)
        assert "На чьё имя" in step3, f"Expected name prompt, got: {step3!r}"

        # Step 4: type name → service picker shown (entering_service).
        bot.reset()
        await dp.feed_update(
            bot, _make_text_update("Паша", user_id=client_tg)
        )
        step4 = _extract_send_text(bot)
        assert "Выберите услугу тапом" in step4, (
            f"5.27 FEAT: with services in DB, must show inline picker. "
            f"Got: {step4!r}"
        )
        # Both services present + 'Своя услуга' fallback.
        svc_btn = await _find_button_by_label(bot, "Стрижка")
        assert svc_btn is not None, "Стрижка button in picker"
        svc2_btn = await _find_button_by_label(bot, "Окрашивание")
        assert svc2_btn is not None, "Окрашивание button in picker"
        custom_btn = await _find_button_by_label(bot, "Своя услуга")
        assert custom_btn is not None, "✏️ Своя услуга fallback button"

        # Step 5: tap 'Окрашивание' → summary with service name (confirming).
        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(svc2_btn, user_id=client_tg)
        )
        step5 = _extract_send_text(bot)
        assert "Подтвердите запись" in step5, f"Expected summary, got: {step5!r}"
        assert "Окрашивание" in step5, f"Service name in summary, got: {step5!r}"
        assert "Паша" in step5, f"Client name in summary, got: {step5!r}"

        # Step 6: tap ✅ → booking created ('Вы записаны').
        confirm_btn = await _find_button_by_label(bot, "Подтвердить")
        assert confirm_btn is not None, "✅ Подтвердить button on summary"
        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(confirm_btn, user_id=client_tg)
        )
        step6 = _extract_send_text(bot)
        assert "Вы записаны" in step6, f"Expected success, got: {step6!r}"

        # Verify Booking persisted with correct service_id + duration.
        from bot.models import Booking

        async with session_factory() as session:
            booking = await session.scalar(
                select(Booking).where(Booking.master_id == ctx["master_id"])
            )
        assert booking is not None, "Booking must be persisted"
        assert booking.service_id == ctx["service2_id"], (
            "service_id in DB must match 'Окрашивание' (120 min) — "
            "_build_end_at uses service.duration_minutes"
        )
        assert booking.service_title_snapshot == "Окрашивание"
        assert booking.client_name_snapshot == "Паша"
        # end_at - start_at should equal 120 min (service duration).
        duration = (booking.end_at - booking.start_at).total_seconds() / 60
        assert duration == 120, (
            f"end_at - start_at must be 120 min (Окрашивание duration), "
            f"got {duration} min — service_id not propagated to _build_end_at?"
        )


@pytest.mark.asyncio
async def test_booking_flow_custom_service_text_uses_default_duration(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """5.27 FEAT E2E: /slots → calendar → slot → name → '✏️ Своя услуга' →
    typed text → ✅ → booking with service_id=None + default duration
    (SERVICE_DEFAULT_DURATION_MIN).

    Verifies the legacy fallback path: 'Своя услуга' button keeps state in
    entering_service, service_msg catches the next text message, service_id
    stays None (or reset to None — F1 fix), _build_end_at uses default.

    F1 regression (stale service_id leaking from previous picker tap) is
    covered by unit tests in test_client_handlers.py
    (test_service_msg_clears_stale_service_id_from_state). This integration
    test focuses on the happy path of the custom-service fallback.
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        ctx = await _seed_workday_tomorrow(session_factory)

        client_tg = 999_888_777

        # Full flow: /slots → calendar → slot → name.
        await dp.feed_update(bot, _make_text_update("/slots", user_id=client_tg))
        assert "Выберите дату" in _extract_send_text(bot)

        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(
            bot, _make_calendar_day_update(tomorrow, user_id=client_tg)
        )
        slot_btn = await _find_button_by_label(bot, "10:00")
        assert slot_btn is not None

        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(slot_btn, user_id=client_tg)
        )
        assert "На чьё имя" in _extract_send_text(bot)

        bot.reset()
        await dp.feed_update(bot, _make_text_update("Паша", user_id=client_tg))
        assert "Выберите услугу тапом" in _extract_send_text(bot)

        # Tap '✏️ Своя услуга' → ask for text.
        custom_btn = await _find_button_by_label(bot, "Своя услуга")
        assert custom_btn is not None
        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(custom_btn, user_id=client_tg)
        )
        assert "Напишите услугу" in _extract_send_text(bot)

        # Type custom service text.
        bot.reset()
        await dp.feed_update(
            bot, _make_text_update("Борода + стрижка", user_id=client_tg)
        )
        summary = _extract_send_text(bot)
        assert "Подтвердите запись" in summary
        assert "Борода + стрижка" in summary

        # Tap ✅.
        confirm_btn = await _find_button_by_label(bot, "Подтвердить")
        assert confirm_btn is not None
        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(confirm_btn, user_id=client_tg)
        )
        assert "Вы записаны" in _extract_send_text(bot)

        # Verify booking: service_id is None, end_at = start + default duration.
        from bot.config import get_settings
        from bot.models import Booking

        async with session_factory() as session:
            booking = await session.scalar(
                select(Booking).where(Booking.master_id == ctx["master_id"])
            )
        assert booking is not None
        assert booking.service_id is None, (
            "Custom text path must NOT set service_id — _build_end_at uses "
            "SERVICE_DEFAULT_DURATION_MIN"
        )
        assert booking.service_title_snapshot == "Борода + стрижка"
        default_min = get_settings().SERVICE_DEFAULT_DURATION_MIN
        duration = (booking.end_at - booking.start_at).total_seconds() / 60
        assert duration == default_min, (
            f"end_at - start_at must be {default_min} min (default), "
            f"got {duration}"
        )
