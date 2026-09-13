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

    async def edit_message_reply_markup(self, *args: Any, **kwargs: Any) -> Any:
        """Stub for bot.edit_message_reply_markup (W3 cancel_msg strip of the
        ✅/❌ summary keyboard, S3 5.52 service_msg strip of the previous
        service picker). Records the aiogram method object so tests can
        assert the strip happened; returns True (handler ignores result).
        """
        from aiogram.methods import EditMessageReplyMarkup

        self.calls.append(
            EditMessageReplyMarkup(
                chat_id=kwargs.get("chat_id") or (args[0] if args else 1),
                message_id=kwargs.get("message_id") or 1,
                reply_markup=kwargs.get("reply_markup"),
            )
        )
        return True

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
            # B.13: first_name="" (empty string) keeps integration tests on the
            # FALLBACK text-input path — _client_first_name strips to "" (falsy)
            # → slot_30_cb takes the entering_name branch (text input), not the
            # pre-fill branch ([✅ Да, это я] inline keyboard). Pydantic rejects
            # first_name=None (User.first_name is required str). The pre-fill UX
            # is covered by unit tests in test_client_handlers.py — integration
            # tests focus on the booking flow + service_id wiring, not on the
            # name-input UX variant.
            from_user=User(id=user_id, is_bot=False, first_name=""),
            text=text,
        ),
    )


def _extract_send_text(bot: Any) -> str:
    """Extract text from the LAST SendMessage call (integration tests
    typically assert on the final message after multi-step flow).
    """
    text: str = bot.last_text
    return text


def _extract_all_send_texts(bot: Any) -> list[str]:
    """Return text of ALL SendMessage calls in order (B.13 helper).

    confirm_cb now sends 2 SendMessage calls — (1) '✅ Вы записаны' with
    post_booking_keyboard (inline), (2) '👇 Кнопки внизу' with reply keyboard
    (via _restore_reply_keyboard_async). `last_text` returns the LAST one
    ('👇 Кнопки внизу'), but tests need to assert on the FIRST ('Вы записаны').
    This helper returns both so tests can pick by index or by substring match.
    """
    from aiogram.methods import EditMessageText, SendMessage

    return [
        getattr(c, "text", "") or ""
        for c in bot.calls
        if isinstance(c, SendMessage | EditMessageText)
    ]


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
            # B.13: first_name="" mirrors _make_text_update (fallback path).
            from_user=User(id=user_id, is_bot=False, first_name=""),
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

        # Step 0: /openweek → week picker (Session 5.64, пункт 1).
        await dp.feed_update(bot, _make_text_update("/openweek"))
        step0_text = _extract_send_text(bot)
        assert "Шаг 1: выберите неделю" in step0_text, (
            f"Step 0 must show week picker prompt; got: {step0_text!r}"
        )

        # Step 1: tap [✅ Выбрать эту неделю] → start picker (Шаг 2).
        select_button = await _find_button_by_label(bot, "✅ Выбрать эту неделю")
        assert select_button is not None, "Expected '✅ Выбрать эту неделю' button"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(select_button))
        step1_text = _extract_send_text(bot)
        assert "Шаг 2" in step1_text

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
        # Баг 3 (Session 2026-09-13): success_lines используют 📅 prefix (не ✅),
        # чтобы визуально отличать summary от inline-кнопок ✏️.
        assert "📅" in summary, f"Expected 📅 in summary, got: {summary!r}"
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
    """/openweek → step 0 (week picker) → [❌ Отмена] → state cleared +
    'Открытие недели отменено' message + menu shown.

    Verifies admin_openweek_cancel_cb dispatch on F.data == 'admin_openweek_cancel'
    with StateFilter(AdminStates) — catches all admin FSM states including
    opening_week_week (Session 5.64, пункт 1). Pre-5.64 the [❌ Отмена] button
    only appeared on Шаг 3 days keyboard — now it's on week picker too.
    """
    dp, bot = integration_dispatcher
    await _seed_admin(session_factory)

    # Шаг 1: /openweek → week picker (Session 5.64).
    await dp.feed_update(bot, _make_text_update("/openweek"))
    step0_text = _extract_send_text(bot)
    assert "Шаг 1: выберите неделю" in step0_text, (
        f"Step 0 must show week picker prompt; got: {step0_text!r}"
    )
    cancel_btn = await _find_button_by_label(bot, "❌ Отмена")
    assert cancel_btn is not None, "❌ Отмена button on week picker (step 0)"

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

        # Four active services — picker shows all 4 + "Своя услуга" (matches prod
        # after 2026-08-30 sync: Стрижка 60, Окрашивание 120, Окрашивание и стрижка
        # 120, Мелирование 120).
        svc1 = Service(business_id=biz.id, name="Стрижка", duration_minutes=60)
        svc2 = Service(business_id=biz.id, name="Окрашивание", duration_minutes=120)
        svc3 = Service(business_id=biz.id, name="Окрашивание и стрижка", duration_minutes=120)
        svc4 = Service(business_id=biz.id, name="Мелирование", duration_minutes=120)
        session.add_all([svc1, svc2, svc3, svc4])
        await session.commit()

        return {
            "business_id": biz.id,
            "master_id": master.id,
            "workday_id": wd.id,
            "service1_id": svc1.id,
            "service2_id": svc2.id,
            "service3_id": svc3.id,
            "service4_id": svc4.id,
        }


async def _seed_today_with_booking(
    session_factory: Any,
    *,
    admin_id: int = ADMIN_TG_ID,
) -> dict[str, Any]:
    """Seed business + master + workday TODAY (10-12) + service + client + booking
    TODAY 11:00-12:00 (confirmed). Returns booking_id for /today → [🔄 Перенести].

    Used by W4 integration test: admin_move flow requires a booking on /today
    so cmd_today renders admin_today_keyboard with [🔄 Перенести] button.
    """
    from datetime import time as dt_time
    from decimal import Decimal

    from bot.models import Booking, Service

    async with session_factory() as session:
        biz = Business(name="Test", telegram_owner_id=admin_id, timezone=TZ)
        session.add(biz)
        await session.flush()
        master = Master(business_id=biz.id, name="T", telegram_id=admin_id, role="owner")
        session.add(master)
        await session.flush()

        today_local = datetime.now(ZoneInfo(TZ)).date()
        wd = WorkDay(
            master_id=master.id,
            work_date=today_local,
            start_time=dt_time(10, 0),
            end_time=dt_time(12, 0),
            is_active=True,
            max_concurrent_clients=1,
        )
        session.add(wd)
        await session.flush()

        svc = Service(business_id=biz.id, name="Стрижка", duration_minutes=60, price=Decimal("0"))
        session.add(svc)
        await session.flush()

        client = Client(telegram_id=999888777, name="Test Client")
        session.add(client)
        await session.flush()

        # Booking today 11:00-12:00 LOCAL → UTC (TZ-aware). freeze_time in tests
        # is 2026-08-25 14:00 UTC; today_local computed from TZ (Europe/Moscow).
        start_local = datetime.combine(today_local, dt_time(11, 0), tzinfo=ZoneInfo(TZ))
        end_local = datetime.combine(today_local, dt_time(12, 0), tzinfo=ZoneInfo(TZ))
        booking = Booking(
            business_id=biz.id,
            master_id=master.id,
            client_id=client.id,
            service_id=svc.id,
            service_title_snapshot="Стрижка",
            service_price_snapshot=Decimal("0"),
            client_name_snapshot="Test Client",
            start_at=start_local.astimezone(UTC),
            end_at=end_local.astimezone(UTC),
            status="confirmed",
        )
        session.add(booking)
        await session.commit()
        return {"booking_id": booking.id, "client_telegram_id": 999888777}


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
    """Session 5.27 FEAT E2E (reordered in 5.29 Task 2; phone step removed 5.50):
    /slots → calendar → service picker (tap 'Окрашивание') → slot picker →
    name → ✅ → booking created with service_id set.

    Flow (Session 5.29 Task 2 — FSM reorder, услуга ДО слота; phone step
    removed in 5.50 — name → confirming directly):
    1. /slots → SimpleCalendar (selecting_date)
    2. tap tomorrow → service picker (entering_service)
    3. tap 'Окрашивание' → slot picker (selecting_slot)
    4. tap 10:00 slot → 'На чьё имя?' (entering_name)
    5. type name → summary (confirming, phone step removed)
    6. tap ✅ → booking created

    Verifies:
    - All handlers dispatch correctly (cmd_slots → simple_calendar_cb →
      service_picker_cb → slot_30_cb → name_msg → confirm_cb)
    - service_picker_keyboard shown with both seeded services + 'Своя услуга'
    - Tap service → slot picker filtered by service.duration_minutes (120)
    - Tap slot → name prompt → type name → Tap ✅ →
      BookingCreate.service_id is the UUID (not None) — _build_end_at uses
      service.duration_minutes
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

        # Step 2: tap tomorrow (2026-08-26) in calendar → service picker.
        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow, user_id=client_tg))
        step2 = _extract_send_text(bot)
        assert "Выберите услугу" in step2, (
            f"5.29 Task 2: with services in DB, must show service picker after date. Got: {step2!r}"
        )
        # All 4 services present. Session 5.51: 'Своя услуга' REMOVED (free
        # text has no duration → wrong slot grid); only master's own services.
        svc_btn = await _find_button_by_label(bot, "Стрижка")
        assert svc_btn is not None, "Стрижка button in picker"
        svc2_btn = await _find_button_by_label(bot, "Окрашивание")
        assert svc2_btn is not None, "Окрашивание button in picker"
        svc3_btn = await _find_button_by_label(bot, "Окрашивание и стрижка")
        assert svc3_btn is not None, "Окрашивание и стрижка button in picker"
        svc4_btn = await _find_button_by_label(bot, "Мелирование")
        assert svc4_btn is not None, "Мелирование button in picker"
        custom_btn = await _find_button_by_label(bot, "Своя услуга")
        assert custom_btn is None, (
            "5.51: 'Своя услуга' free-text button must be gone from the picker"
        )

        # Step 3: tap 'Окрашивание' → slot picker (selecting_slot).
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(svc2_btn, user_id=client_tg))
        step3 = _extract_send_text(bot)
        assert "Выберите время" in step3, f"5.29 Task 2: service tap → slot picker. Got: {step3!r}"
        slot_btn = await _find_button_by_label(bot, "10:00")
        assert slot_btn is not None, (
            f"Expected 10:00 slot button after service tap. Got text: {step3!r}"
        )

        # Step 4: tap 10:00 slot → 'На чьё имя?' prompt (entering_name).
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(slot_btn, user_id=client_tg))
        step4 = _extract_send_text(bot)
        assert "На чьё имя" in step4, f"Expected name prompt, got: {step4!r}"

        # Step 5: type name → summary (confirming, phone step removed).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("Паша", user_id=client_tg))
        step5 = _extract_send_text(bot)
        assert "Подтвердите запись" in step5, f"name_msg → confirming (summary). Got: {step5!r}"
        assert "Окрашивание" in step5
        assert "Паша" in step5

        # Step 6: tap ✅ → booking created ('Вы записаны').
        confirm_btn = await _find_button_by_label(bot, "Подтвердить")
        assert confirm_btn is not None, "✅ Подтвердить button on summary"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(confirm_btn, user_id=client_tg))
        # B.13: confirm_cb sends 2 messages — (1) '✅ Вы записаны' BARE text
        # (5.51: post_booking_keyboard inline removed — duplicates of the
        # always-on reply keyboard showed as dead buttons), (2) '👇 Кнопки
        # внизу' with reply keyboard (via _restore_reply_keyboard_async).
        step6_texts = _extract_all_send_texts(bot)
        assert any("Вы записаны" in t for t in step6_texts), (
            f"Expected success message, got: {step6_texts!r}"
        )
        # 5.51: no inline keyboard on the success message (dead-button fix).
        from aiogram.methods import SendMessage

        success_calls = [
            c
            for c in bot.calls
            if isinstance(c, SendMessage) and "Вы записаны" in (getattr(c, "text", "") or "")
        ]
        assert success_calls, "success SendMessage call must be recorded"
        assert all(getattr(c, "reply_markup", None) is None for c in success_calls), (
            "5.51: 'Вы записаны' must be bare text — no inline post_booking_keyboard"
        )

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
async def test_booking_flow_typed_text_in_service_step_hints_and_flow_continues(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 5.51 E2E: free-text service DISABLED. Typed text in the
    service step → hint 'Пожалуйста, выберите услугу кнопкой' — and the
    flow is NOT broken: the user can still tap a service button right
    after the hint and finish the booking.

    Was test_booking_flow_custom_service_text_uses_default_duration (5.27):
    '✏️ Своя услуга' → typed text → booking with service_id=None + default
    duration. That path is gone — a free-text service has no known
    duration_minutes → the slot grid and /today would silently use
    SERVICE_DEFAULT_DURATION_MIN (60) and lie about the reserved time
    (user report 2026-09-11).

    Flow verified here:
    1. /slots → SimpleCalendar (selecting_date)
    2. tap tomorrow → service picker (entering_service)
    3. type 'Борода + стрижка' → hint (STILL entering_service — state not
       cleared, not advanced; the picker message above is still actionable)
    4. tap 'Стрижка' → slot picker (selecting_slot)
    5. tap 10:00 slot → 'На чьё имя?' (entering_name)
    6. type name → summary (confirming)
    7. tap ✅ → booking created with the TAPPED service_id (not the typed
       text, not None — free-text can no longer reach BookingCreate)
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        ctx = await _seed_workday_tomorrow(session_factory)

        client_tg = 999_888_777

        # Step 1: /slots → SimpleCalendar (selecting_date).
        await dp.feed_update(bot, _make_text_update("/slots", user_id=client_tg))
        assert "Выберите дату" in _extract_send_text(bot)

        # Step 2: tap tomorrow → service picker (entering_service).
        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow, user_id=client_tg))
        assert "Выберите услугу" in _extract_send_text(bot)
        # Grab the service button NOW — after the hint (Step 3) the LAST
        # message has no keyboard, and _find_button_by_label only looks at
        # the last message. Tapping a button from the message above mirrors
        # real Telegram (the picker message is still actionable).
        svc_btn = await _find_button_by_label(bot, "Стрижка")
        assert svc_btn is not None, "Стрижка button in the service picker"

        # Step 3: type text instead of tapping → hint, state NOT advanced.
        # 5.52 (S3): the hint is self-healing — same answer carries a FRESH
        # service picker (deleted/scroll-away picker no longer a dead end),
        # and the PREVIOUS picker message gets its keyboard stripped
        # (bot.edit_message_reply_markup — recorded by RecordingBot).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("Борода + стрижка", user_id=client_tg))
        step3 = _extract_send_text(bot)
        assert "выберите услугу кнопкой" in step3, f"5.51: typed text → button hint. Got: {step3!r}"
        from aiogram.methods import EditMessageReplyMarkup

        strip_calls = [c for c in bot.calls if isinstance(c, EditMessageReplyMarkup)]
        assert strip_calls, "S3: previous picker keyboard must be stripped on typed text"
        fresh_picker_btn = await _find_button_by_label(bot, "Стрижка")
        assert fresh_picker_btn is not None, (
            "S3 (5.52): hint answer must carry a FRESH tappable service picker"
        )

        # Step 4: tap 'Стрижка' (from the picker message above) right after
        # the hint → slot picker — the flow survived the typed text
        # (entering_service still active).
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(svc_btn, user_id=client_tg))
        step4 = _extract_send_text(bot)
        assert "Выберите время" in step4, (
            f"5.51: service tap after text hint → slot picker. Got: {step4!r}"
        )
        slot_btn = await _find_button_by_label(bot, "10:00")
        assert slot_btn is not None, "10:00 slot button after service tap"

        # Step 5: tap 10:00 slot → 'На чьё имя?' (entering_name).
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(slot_btn, user_id=client_tg))
        assert "На чьё имя" in _extract_send_text(bot)

        # Step 6: type name → summary (confirming, phone step removed).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("Паша", user_id=client_tg))
        step6 = _extract_send_text(bot)
        assert "Подтвердите запись" in step6, f"name_msg → confirming (summary). Got: {step6!r}"
        # Summary shows the TAPPED service, not the typed text.
        assert "Стрижка" in step6
        assert "Борода + стрижка" not in step6
        assert "Паша" in step6

        # Step 7: tap ✅.
        confirm_btn = await _find_button_by_label(bot, "Подтвердить")
        assert confirm_btn is not None
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(confirm_btn, user_id=client_tg))
        # B.13: confirm_cb sends 2 messages — '✅ Вы записаны' (bare text,
        # 5.51) + '👇 Кнопки внизу' (reply keyboard restore).
        step7_texts = _extract_all_send_texts(bot)
        assert any("Вы записаны" in t for t in step7_texts), (
            f"Expected success message, got: {step7_texts!r}"
        )

        # Verify booking: the TAPPED service_id (Стрижка, 60 min) — free-text
        # 'Борода + стрижка' must never reach BookingCreate.
        from bot.models import Booking

        async with session_factory() as session:
            booking = await session.scalar(
                select(Booking).where(Booking.master_id == ctx["master_id"])
            )
        assert booking is not None
        assert booking.service_id == ctx["service1_id"], (
            "Booking must carry the tapped 'Стрижка' service_id — the typed "
            "free-text must NOT leak into the booking (5.51)"
        )
        assert booking.service_title_snapshot == "Стрижка"
        duration = (booking.end_at - booking.start_at).total_seconds() / 60
        assert duration == 60, (
            f"end_at - start_at must be 60 min (Стрижка duration), got {duration}"
        )


@pytest.mark.asyncio
async def test_cancel_command_works_in_service_step(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """5.52 (review W1): /cancel typed at the service step must reach
    cancel_msg (Command filter, StateFilter("*")), NOT be swallowed by
    service_msg. Pre-5.52 the StateFilter-only catch-all ate it — the user
    got a re-rendered picker instead of cancellation (and at the name step
    /cancel could become client_name). This test pins the real dispatch
    through dp.feed_update — unit tests bypass router filters.

    Also verifies: cancel_msg strips the tracked service picker (S3/W3
    msg_id pattern — EditMessageReplyMarkup recorded), state is cleared
    (next plain text hits no_state_fallback's "Начните запись через /book").
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        await _seed_workday_tomorrow(session_factory)
        client_tg = 999_888_777

        # Step 1-2: /slots → tomorrow tap → service picker (entering_service).
        await dp.feed_update(bot, _make_text_update("/slots", user_id=client_tg))
        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow, user_id=client_tg))
        assert "Выберите услугу" in _extract_send_text(bot)

        # Step 3: /cancel at the service step → cancel_msg wins dispatch.
        bot.reset()
        await dp.feed_update(bot, _make_text_update("/cancel", user_id=client_tg))
        texts = _extract_all_send_texts(bot)
        assert any("Ввод отменён" in t for t in texts), (
            f"W1: /cancel must reach cancel_msg. Got: {texts!r}"
        )
        assert not any("выберите услугу кнопкой" in t for t in texts), (
            "W1: /cancel must NOT hit service_msg (fresh-picker re-render) — "
            "that means the catch-all still swallows commands"
        )

        # The tracked service picker was stripped (S3 tracker + cancel_msg loop).
        from aiogram.methods import EditMessageReplyMarkup

        strip_calls = [c for c in bot.calls if isinstance(c, EditMessageReplyMarkup)]
        assert strip_calls, "5.52: cancel_msg must strip the tracked service picker"

        # State is cleared: plain text now hits no_state_fallback (State(None)).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("ещё текст", user_id=client_tg))
        assert "Начните запись через /book" in _extract_send_text(bot), (
            "After ❌ Отмена the FSM must be State(None) — plain text hits the fallback"
        )


@pytest.mark.asyncio
async def test_cancel_button_text_works_in_admin_move_selecting_date_state(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 2026-09-13 (W4 fix): ❌ Отмена tap в AdminMoveStates.selecting_date
    → admin_cancel_msg wins dispatch (state.clear() + «Админ-режим отменён»), NOT
    client_router cancel_msg (booking-specific «Ввод отменён. /book» hint —
    misleading для admin который делал /today → 🔄 Перенести, не /book).

    Pre-fix: admin_cancel_msg filter был `StateFilter(AdminStates)` only — НЕ
    покрывал AdminMoveStates (3 states: selecting_date/selecting_slot/confirming).
    ❌ Отмена в admin_move flow проваливался в client_router cancel_msg (StateFilter("*")
    матчит AdminMoveStates) → hint «Ввод отменён. /book чтобы начать заново»
    (booking-specific, misleading). Escape работал (state.clear срабатывал), но
    hint был неточный. Fix: admin_cancel_msg filter расширен на
    or_f(StateFilter(AdminStates), StateFilter(AdminMoveStates)) — единый escape
    hatch для всех 15 admin FSM states.

    Admin_move flow — callback-driven (4 callback handlers admin.py:2742/2901/
    2985/3156), text input НЕ expected. ❌ Отмена как text — единственный message
    path. or_f НЕ перехватит admin_move callback handlers (callback vs message —
    разные buckets в aiogram 3.x dispatch).

    Regression guard: integration test через dp.feed_update ловит filter-matching
    баги которые unit tests не ловят.
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        await _seed_today_with_booking(session_factory)

        # Step 1: /today → admin_today_keyboard with [🔄 Перенести] button.
        await dp.feed_update(bot, _make_text_update("/today", user_id=ADMIN_TG_ID))
        today_text = _extract_send_text(bot)
        assert "Записи на сегодня" in today_text, f"expected today list, got: {today_text!r}"

        # Step 2: tap [🔄 Перенести] → AdminMoveStates.selecting_date (calendar).
        move_btn = await _find_button_by_label(bot, "🔄")
        assert move_btn is not None, (
            f"Expected [🔄 Перенести] button. Got markup: {bot.last_reply_markup!r}"
        )
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(move_btn, user_id=ADMIN_TG_ID))
        cal_text = _extract_send_text(bot)
        assert "Выберите новую дату" in cal_text or "Выберите дату" in cal_text, (
            f"After [🔄 Перенести] tap must be in AdminMoveStates.selecting_date. Got: {cal_text!r}"
        )

        # Step 3 (THE TEST): type "❌ Отмена" → admin_cancel_msg wins (NOT client cancel_msg).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("❌ Отмена", user_id=ADMIN_TG_ID))
        texts = _extract_all_send_texts(bot)
        assert any("Админ-режим отменён" in t for t in texts), (
            f"W4: ❌ Отмена in AdminMoveStates must reach admin_cancel_msg. Got: {texts!r}"
        )
        # CRITICAL: «❌ Отмена» НЕ должно попасть в client_router cancel_msg
        # (booking-specific hint «Ввод отменён. /book» — misleading для admin).
        assert not any("Ввод отменён" in t and "/book" in t for t in texts), (
            "W4: ❌ Отмена in AdminMoveStates must NOT fall through to client_router "
            f"cancel_msg (booking-specific hint). Got: {texts!r}"
        )

        # State is cleared: plain text now hits admin_no_state_catchall_text.
        bot.reset()
        await dp.feed_update(bot, _make_text_update("ещё текст", user_id=ADMIN_TG_ID))
        post_text = _extract_send_text(bot)
        assert "/menu" in post_text or "Меню" in post_text, (
            "After ❌ Отмена the FSM must be State(None) — admin plain text hits "
            f"admin_no_state_catchall_text. Got: {post_text!r}"
        )


@pytest.mark.asyncio
async def test_admin_state_catchall_text_works_in_admin_move_selecting_date_state(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 2026-09-13 (W5 fix): arbitrary text в AdminMoveStates.selecting_date
    → admin_state_catchall_text wins (hint "Используйте /cancel"), NOT silent
    failure (bot молчит, без fix нет handler для text в AdminMoveStates).

    Pre-fix: admin_state_catchall_text filter был `StateFilter(AdminStates)` only
    (admin.py:4711) — НЕ покрывал AdminMoveStates (3 states: selecting_date/
    selecting_slot/confirming). arbitrary text в admin_move flow НЕ матчит ни
    одного @router.message в admin_router (все AdminMoveStates handlers
    callback_query — calendar 2742, slot 2901, confirm 2985, cancel 3156) И НЕ
    матчит client_router no_state_fallback (StateFilter(None), client.py:2423 —
    НЕ StateFilter("*")) → бот МОЛЧАЛ (silent failure). Fix: admin_state_catchall_text
    filter расширен на StateFilter(AdminStates, AdminMoveStates) — mirror W4 fix
    (admin_cancel_msg line 4657).

    Test mirror W4 test_cancel_button_text_works_in_admin_move_selecting_date_state
    но с arbitrary text "12" (НЕ "❌ Отмена", НЕ /cmd).

    Regression guards:
    1. Bot отвечает "Используйте /cancel" (НЕ молчит, НЕ client hint "/book")
    2. State сохраняется AdminMoveStates.selecting_date (catchall НЕ чистит state —
       "❌ Отмена" в следующем step всё ещё попадает в admin_cancel_msg →
       "Админ-режим отменён", НЕ в admin_no_state_catchall_text → "/menu")
    3. Catchall НЕ поглощает legitimate calendar tap (callback vs message —
       разные buckets в aiogram 3.x dispatch, явно отмечено в W4 docstring)
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        await _seed_today_with_booking(session_factory)

        # Step 1: /today → admin_today_keyboard with [🔄 Перенести] button.
        await dp.feed_update(bot, _make_text_update("/today", user_id=ADMIN_TG_ID))
        today_text = _extract_send_text(bot)
        assert "Записи на сегодня" in today_text, f"expected today list, got: {today_text!r}"

        # Step 2: tap [🔄 Перенести] → AdminMoveStates.selecting_date (calendar).
        move_btn = await _find_button_by_label(bot, "🔄")
        assert move_btn is not None, (
            f"Expected [🔄 Перенести] button. Got markup: {bot.last_reply_markup!r}"
        )
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(move_btn, user_id=ADMIN_TG_ID))
        cal_text = _extract_send_text(bot)
        assert "Выберите новую дату" in cal_text or "Выберите дату" in cal_text, (
            f"After [🔄 Перенести] tap must be in AdminMoveStates.selecting_date. Got: {cal_text!r}"
        )

        # Step 3 (THE TEST): type arbitrary text "12" → admin_state_catchall_text
        # wins (NOT silent failure, NOT client_router no_state_fallback "/book").
        bot.reset()
        await dp.feed_update(bot, _make_text_update("12", user_id=ADMIN_TG_ID))
        texts = _extract_all_send_texts(bot)
        assert texts, (
            "W5: arbitrary text в AdminMoveStates must NOT be silent — bot должен "
            f"ответить hint. Pre-fix бот молчал. Got: {texts!r}"
        )
        assert any("Используйте /cancel" in t for t in texts), (
            "W5: arbitrary text в AdminMoveStates must reach admin_state_catchall_text "
            f"(hint 'Используйте /cancel'). Got: {texts!r}"
        )
        # CRITICAL: arbitrary text НЕ должно попасть в client_router no_state_fallback
        # (booking-specific hint "Начните запись через /book" — misleading для admin).
        assert not any("Начните запись через /book" in t for t in texts), (
            "W5: arbitrary text в AdminMoveStates must NOT fall through to "
            f"client_router no_state_fallback. Got: {texts!r}"
        )

        # Step 4 (regression guard): state preserved — "❌ Отмена" в следующем
        # step всё ещё попадает в admin_cancel_msg (W4) → "Админ-режим отменён",
        # НЕ в admin_no_state_catchall_text → "/menu для меню" без "Админ-режим"
        # (state был бы очищен если catchall его чистил, но catchall НЕ трогает state).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("❌ Отмена", user_id=ADMIN_TG_ID))
        post_texts = _extract_all_send_texts(bot)
        assert any("Админ-режим отменён" in t for t in post_texts), (
            "W5 regression: after arbitrary text hint, state must still be "
            "AdminMoveStates — '❌ Отмена' must reach admin_cancel_msg (W4). "
            f"Got: {post_texts!r}"
        )


@pytest.mark.asyncio
async def test_cancel_button_text_works_in_entering_name_state(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 2026-09-13 (admin cancel button F1 fix): ❌ Отмена tap в
    BookingStates.entering_name → cancel_msg wins dispatch (state.clear() +
    «Ввод отменён»), NOT name_msg (data corruption — booking named «❌ Отмена»).

    Pre-fix: name_msg had filter `~F.text.startswith("/")` only — «❌ Отмена»
    не начинается с "/", name_msg сматчит первым (registration order: name_msg
    1417 ПЕРЕД cancel_msg 2086) → client_name = "❌ Отмена" → data corruption.

    Fix: name_msg filter расширен `F.text != "❌ Отмена"` (mirror для service_msg
    на 1762). cancel_msg filter расширен `or_f(F.text == "❌ Отмена",
    Command("cancel"))`. Теперь ❌ Отмена в entering_name проваливается через
    name_msg (не матчит) → cancel_msg (матчит) → state.clear() + booking-cancel
    message.

    Regression guard: integration test через dp.feed_update ловит
    filter-matching баги которые unit tests (direct handler invocation) не
    ловят (reviewer S1 — unit test вызывает admin_cancel_msg напрямую, не
    через router dispatch, поэтому F1 изначально не был пойман).
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        await _seed_workday_tomorrow(session_factory)
        client_tg = 999_888_777

        # Step 1: /book → date picker (selecting_date).
        await dp.feed_update(bot, _make_text_update("/book", user_id=client_tg))
        assert "Выберите дату" in _extract_send_text(bot)

        # Step 2: tap tomorrow → service picker.
        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow, user_id=client_tg))
        assert "Выберите услугу" in _extract_send_text(bot)

        # Step 3: tap first service → slot picker.
        svc_btn = await _find_button_by_label(bot, "Стрижка")
        assert svc_btn is not None, "Стрижка service button"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(svc_btn, user_id=client_tg))
        assert "Выберите время" in _extract_send_text(bot)

        # Step 4: tap 10:00 slot → entering_name prompt.
        slot_btn = await _find_button_by_label(bot, "10:00")
        assert slot_btn is not None, "10:00 slot button"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(slot_btn, user_id=client_tg))
        assert "На чьё имя" in _extract_send_text(bot), "must be in entering_name state"

        # Step 5 (THE TEST): type "❌ Отмена" → cancel_msg wins (NOT name_msg).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("❌ Отмена", user_id=client_tg))
        texts = _extract_all_send_texts(bot)
        assert any("Ввод отменён" in t for t in texts), (
            f"F1 fix: ❌ Отмена in entering_name must reach cancel_msg. Got: {texts!r}"
        )
        # CRITICAL: «❌ Отмена» НЕ должно стать client_name (data corruption).
        assert not any("Подтвердите запись" in t for t in texts), (
            "F1: ❌ Отмена must NOT reach name_msg — that would create a booking "
            f"named '❌ Отмена'. Got summary text: {texts!r}"
        )
        assert not any("❌ Отмена" in t and "Подтвердите" in t for t in texts), (
            "F1: ❌ Отмена must NOT appear as client_name in booking summary"
        )

        # State is cleared: plain text now hits no_state_fallback (State(None)).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("ещё текст", user_id=client_tg))
        assert "Начните запись через /book" in _extract_send_text(bot), (
            "After ❌ Отмена the FSM must be State(None) — plain text hits the fallback"
        )


@pytest.mark.asyncio
async def test_cancel_button_text_works_in_entering_service_name_state(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 2026-09-13 (admin cancel button, code-review pass 2 F1+F2 fix):
    ❌ Отмена tap в AdminStates.entering_service_name → admin_cancel_msg wins
    dispatch (state.clear() + «Админ-режим отменён»), NOT admin_service_name_msg
    (data corruption — услуга с именем «❌ Отмена» создавалась в БД).

    Pre-fix (pass 2 F1): admin_service_name_msg (admin.py:2559) had filter
    `StateFilter(AdminStates.entering_service_name), F.text, ~F.text.startswith("/")`
    — «❌ Отмена» это текст, не начинается с "/", admin_service_name_msg
    сматчит первым (registration order: admin_service_name_msg ПЕРЕД
    admin_cancel_msg 4630) → service name = "❌ Отмена" → state.set_state(
    entering_service_duration) → если admin введёт число, create_service
    создаст услугу «❌ Отмена» в БД.

    Fix: admin_service_name_msg filter расширен F.text != "❌ Отмена". Mirror
    fix для admin_service_duration_msg (pass 2 F2 — без exclusion admin
    зависал на int("❌ Отмена") ValueError, state stays, escape hatch сломан).

    Regression guard: integration test через dp.feed_update ловит
    filter-matching баги которые unit tests (direct handler invocation) не
    ловят (reviewer S1 — unit test вызывает admin_cancel_msg напрямую, не
    через router dispatch).
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        await _seed_admin(session_factory)

        # Step 1: /menu → admin inline menu (содержит «➕ Добавить услугу»).
        await dp.feed_update(bot, _make_text_update("/menu", user_id=ADMIN_TG_ID))
        menu_text = _extract_send_text(bot)
        assert "Меню" in menu_text, f"expected menu, got: {menu_text!r}"

        # Step 2: tap «➕ Добавить услугу» (или аналогичная) → entering_service_name.
        # admin_inline_menu callback «service_add» → admin_service_add_entry_cb
        # → state.set_state(AdminStates.entering_service_name).
        add_btn = await _find_button_by_label(bot, "Услуги")
        assert add_btn is not None, "«Услуги» button in admin menu"
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(add_btn, user_id=ADMIN_TG_ID))
        # После тапа «Услуги» должен появиться список услуг + кнопка ➕ для добавления.
        services_screen = _extract_send_text(bot)
        # Если кнопка ➕ прямо здесь — тапаем её. Иначе ищем во втором сообщении.
        add_service_btn = await _find_button_by_label(bot, "➕")
        if add_service_btn is None:
            # Может быть «Добавить услугу» или аналогичный label.
            add_service_btn = await _find_button_by_label(bot, "Добавить услугу")
        assert add_service_btn is not None, (
            f"Add-service button (➕ or 'Добавить услугу') not found. "
            f"Services screen: {services_screen!r}"
        )
        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(add_service_btn, user_id=ADMIN_TG_ID)
        )
        name_prompt = _extract_send_text(bot)
        assert "название" in name_prompt.lower() or "Введите" in name_prompt, (
            f"After ➕ tap must be in entering_service_name. Got: {name_prompt!r}"
        )

        # Step 3 (THE TEST): type «❌ Отмена» → admin_cancel_msg wins.
        # НЕ admin_service_name_msg (data corruption — услуга «❌ Отмена»).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("❌ Отмена", user_id=ADMIN_TG_ID))
        texts = _extract_all_send_texts(bot)
        assert any("Админ-режим отменён" in t for t in texts), (
            f"F1 fix: ❌ Отмена in entering_service_name must reach admin_cancel_msg. "
            f"Got: {texts!r}"
        )
        # CRITICAL: «❌ Отмена» НЕ должно стать service name (data corruption).
        assert not any("Введите длительность" in t for t in texts), (
            "F1: ❌ Отмена must NOT reach admin_service_name_msg — that would "
            f"set service name to '❌ Отмена'. Got: {texts!r}"
        )
        assert not any("❌ Отмена" in t and "Название:" in t for t in texts), (
            "F1: ❌ Отмена must NOT appear as service name in duration prompt"
        )

        # State is cleared: plain text now hits admin_no_state_catchall_text.
        bot.reset()
        await dp.feed_update(bot, _make_text_update("ещё текст", user_id=ADMIN_TG_ID))
        post_text = _extract_send_text(bot)
        assert "/menu" in post_text or "Меню" in post_text, (
            "After ❌ Отмена the FSM must be State(None) — admin plain text hits "
            f"admin_no_state_catchall_text. Got: {post_text!r}"
        )


@pytest.mark.asyncio
async def test_cancel_button_text_works_in_entering_service_state(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 2026-09-13 (S1 — mirror test 945 для BookingStates.entering_service):
    ❌ Отмена tap в BookingStates.entering_service → cancel_msg wins dispatch
    (state.clear() + «Ввод отменён»), NOT service_msg (data corruption — service
    picker re-render вместо cancel).

    Pre-fix: service_msg (client.py:1772) filter был `~F.text.startswith("/")` only
    — «❌ Отмена» это text, не начинается с "/", service_msg сматчит первым
    (registration order: service_msg 1772 ПЕРЕД cancel_msg 2086) → re-renders
    service picker (НЕ cancel). Fix: service_msg filter расширен
    `F.text != "❌ Отмена"` (mirror name_msg:1417). Теперь ❌ Отмена проваливается
    через service_msg (не матчит) → cancel_msg (матчит) → state.clear().

    Regression guard: integration test через dp.feed_update ловит filter-matching
    баги которые unit tests (direct handler invocation) не ловят (reviewer S1).
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        await _seed_workday_tomorrow(session_factory)
        client_tg = 999_888_777

        # Step 1: /book → date picker (selecting_date).
        await dp.feed_update(bot, _make_text_update("/book", user_id=client_tg))
        assert "Выберите дату" in _extract_send_text(bot)

        # Step 2: tap tomorrow → service picker (entering_service).
        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow, user_id=client_tg))
        assert "Выберите услугу" in _extract_send_text(bot), "must be in entering_service"

        # Step 3 (THE TEST): type "❌ Отмена" → cancel_msg wins (NOT service_msg).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("❌ Отмена", user_id=client_tg))
        texts = _extract_all_send_texts(bot)
        assert any("Ввод отменён" in t for t in texts), (
            f"S1: ❌ Отмена in entering_service must reach cancel_msg. Got: {texts!r}"
        )
        # CRITICAL: «❌ Отмена» НЕ должно триггерить service_msg (re-render picker).
        assert not any("выберите услугу кнопкой" in t for t in texts), (
            "S1: ❌ Отмена must NOT reach service_msg — that would re-render the "
            f"service picker instead of cancelling. Got: {texts!r}"
        )

        # State is cleared: plain text now hits no_state_fallback (State(None)).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("ещё текст", user_id=client_tg))
        assert "Начните запись через /book" in _extract_send_text(bot), (
            "After ❌ Отмена the FSM must be State(None) — plain text hits the fallback"
        )
