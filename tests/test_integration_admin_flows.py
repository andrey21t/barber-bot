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
import re
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
        # Direct bot.send_message(...) targets: list of (chat_id, text).
        # SEPARATE registry — self.calls не трогаем (helpers
        # _extract_send_text/_extract_all_send_texts фильтруют calls по
        # SendMessage; вливание туда ломало бы 1952 строки ассертов).
        self.sent_direct: list[tuple[int, str]] = []
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
        self.sent_direct.clear()

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
        master AND by _notify_cancelled_clients to notify clients).
        Records (chat_id, text) into self.sent_direct; returns a stub
        Message — handler doesn't await on it.
        """
        from aiogram.types import Chat
        from aiogram.types import Message as AioMessage

        chat_id = kwargs.get("chat_id") or (args[0] if args else 1)
        text = kwargs.get("text", "")
        self.sent_direct.append((chat_id, text))
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
    """/openweek → step 1 (week picker) → /cancel command → state cleared +
    'Админ-режим отменён' message + fresh /openweek re-enters.

    UX-баг 4 (Session 2026-09-14, final): inline ❌ Отмена removed from week
    picker (keyboards/admin.py:admin_week_picker_keyboard) and days keyboard
    (admin_week_days_keyboard), AND reply keyboard ❌ Отмена button removed
    (📋 Меню берёт функцию «отмена + меню»). /cancel command remains as power-
    user text escape — caught by admin_cancel_msg (W4 or_f(F.text == "❌ Отмена",
    Command("cancel")) + StateFilter(AdminStates, AdminMoveStates)). This test
    verifies the /cancel path (text input, not inline button).
    """
    dp, bot = integration_dispatcher
    await _seed_admin(session_factory)

    # Шаг 1: /openweek → week picker (Session 5.64).
    await dp.feed_update(bot, _make_text_update("/openweek"))
    step0_text = _extract_send_text(bot)
    assert "Шаг 1: выберите неделю" in step0_text, (
        f"Step 0 must show week picker prompt; got: {step0_text!r}"
    )

    # Type /cancel command (power-user escape, no UI-кнопки ❌ Отмена после UX-баг 4).
    # W4 admin_cancel_msg catches it via Command("cancel") + StateFilter(AdminStates).
    bot.reset()
    await dp.feed_update(bot, _make_text_update("/cancel"))
    text = _extract_send_text(bot)
    assert "Админ-режим отменён" in text, (
        f"/cancel command must clear state + show 'Админ-режим отменён'; got: {text!r}"
    )

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


async def _seed_workday_tomorrow_tz_edge(
    session_factory: Any,
    *,
    start_time_str: str = "12:00",
    end_time_str: str = "23:30",
) -> dict[str, Any]:
    """P1-B tz-edge seed: WorkDay TOMORROW 12:00–23:30 LOCAL + ONE 30-min
    service. Late-evening window where the last grid slot (23:00) only
    survives the BUG2 duration filter when service is 30 min
    (23:00+30 == 23:30 end, boundary `<=`).
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

        svc = Service(business_id=biz.id, name="Экспресс 30", duration_minutes=30)
        session.add(svc)
        await session.commit()

        return {
            "business_id": biz.id,
            "master_id": master.id,
            "workday_id": wd.id,
            "service_id": svc.id,
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
async def test_booking_tz_edge_2300_slot_visible_in_picker(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """P1-B mini-E2E (coverage plan, critic DEEP_ENOUGH): late window
    12:00–23:30 LOCAL, 30-min service → client slot picker must render the
    boundary slot labeled "23:00" (LOCAL Moscow label, NOT 20:00 UTC).

    Chain under test (service → picker → slot grid):
    service_picker_cb → get_available_slots_30(min_duration_min=30) →
    BUG2 filter boundary `<=` keeps 23:00 (23:00+30 == 23:30 end) →
    slot_30_cb renders TimeSlot30.label. Grid generation is half-open, so
    "23:30" must NOT exist as a button.

    Companion unit test: test_slots.py::test_get_available_slots_30_tz_edge_
    evening_boundary (asserts start_at_utc == 20:00 UTC same date).
    This E2E covers the RENDER path: dispatcher wiring + FSM + keyboard.
    """
    from freezegun import freeze_time

    # Freeze 14:00 UTC = 17:00 MSK — tomorrow fully future for the picker.
    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        await _seed_workday_tomorrow_tz_edge(session_factory)

        client_tg = 999_888_777

        # Step 1: /slots → SimpleCalendar (selecting_date).
        await dp.feed_update(bot, _make_text_update("/slots", user_id=client_tg))
        assert "Выберите дату" in _extract_send_text(bot)

        # Step 2: tap tomorrow → service picker.
        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow, user_id=client_tg))
        step2 = _extract_send_text(bot)
        assert "Выберите услугу" in step2, f"Expected service picker, got: {step2!r}"
        svc_btn = await _find_button_by_label(bot, "Экспресс 30")
        assert svc_btn is not None, "Экспресс 30 button in the service picker"

        # Step 3: tap the 30-min service → slot picker renders the boundary slot.
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(svc_btn, user_id=client_tg))
        step3 = _extract_send_text(bot)
        assert "Выберите время" in step3, f"service tap → slot picker. Got: {step3!r}"
        slot_btn = await _find_button_by_label(bot, "23:00")
        assert slot_btn is not None, (
            "Boundary slot 23:00 (23:00+30 == 23:30 end, `<=`) must be visible "
            "with its LOCAL label — BUG2 off-by-one would hide it. "
            f"Got text: {step3!r}"
        )
        # Half-open grid: slot starting exactly at end_time does not exist.
        assert await _find_button_by_label(bot, "23:30") is None, (
            "Slot 23:30 (== end_time) must not be rendered"
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
async def test_admin_state_catchall_callback_works_in_admin_move_selecting_date_state(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """Session 2026-09-14 (W6 fix): stale callback тап в AdminMoveStates.
    selecting_date → admin_state_catchall_callback wins (callback.answer
    "Используйте /cancel"), NOT silent failure (loading spinner остаётся,
    callback.answer не вызывается — pre-fix бот молчал).

    Pre-fix: admin_state_catchall_callback filter был `StateFilter(AdminStates)`
    only (admin.py:4725) — НЕ покрывал AdminMoveStates (3 states: selecting_date/
    selecting_slot/confirming). stale callback тап в admin_move flow while still
    in AdminMoveStates (например, callback `admin_window_cancel` от adding_slots
    confirm keyboard — handler 1482 требует StateFilter(AdminStates), не матчит
    в AdminMoveStates) → без catchall бот МОЛЧИТ, loading spinner остаётся на
    кнопке. Fix: admin_state_catchall_callback filter расширен на
    StateFilter(AdminStates, AdminMoveStates) — mirror W4 (admin_cancel_msg
    line 4645) и W5 (admin_state_catchall_text line 4699) — единый catchall
    для всех 15 admin FSM states в обоих buckets (message + callback).
    Catchall НЕ поглощает legitimate input (specific CallbackData.filter() +
    registered раньше → top-down first-match в aiogram 3.x), НЕ меняет state,
    НЕ триггерит DB writes.

    Test mirror W5 test_admin_state_catchall_text_works_in_admin_move_selecting_
    date_state но с stale callback (callback_query) вместо arbitrary text
    (message). Test scenario: stale callback `admin_window_cancel` (из
    adding_slots flow) тапнут while in AdminMoveStates.selecting_date —
    НЕ матчит specific AdminMoveStates CallbackData.filter() (calendar/slot/
    confirm), НЕ матчит admin_window_cancel_cb (StateFilter(AdminStates), не
    покрывает AdminMoveStates) → catchall wins.

    Regression guards:
    1. AnswerCallbackQuery вызывается (callback.answer срабатывает — НЕ silent)
    2. Exactly 1 AnswerCallbackQuery call (только catchall ответил, top-down
       first-match — один handler)
    3. Hint text "Используйте /cancel для отмены" (правильный hint)
    4. State сохраняется AdminMoveStates.selecting_date — "❌ Отмена" в
       следующем step всё ещё попадает в admin_cancel_msg → "Админ-режим
       отменён" (catchall НЕ чистит state — callback.answer только)
    """
    from aiogram.methods import AnswerCallbackQuery
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

        # Step 3 (THE TEST): stale callback tap "admin_window_cancel" (old
        # admin_window_cancel callback от previous keyboard, требует
        # StateFilter(AdminStates) — не матчит в AdminMoveStates) → catchall
        # admin_state_catchall_callback wins → callback.answer.
        bot.reset()
        stale_update = Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="2",
                chat_instance=str(ADMIN_TG_ID),
                data="admin_window_cancel",
                from_user=User(id=ADMIN_TG_ID, is_bot=False, first_name=""),
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=ADMIN_TG_ID, type="private"),
                    text="",
                ),
            ),
        )
        await dp.feed_update(bot, stale_update)
        answer_calls = [c for c in bot.calls if isinstance(c, AnswerCallbackQuery)]
        assert answer_calls, (
            "W6: stale callback в AdminMoveStates must NOT be silent — "
            "callback.answer must fire (убирает loading spinner). Pre-fix бот "
            f"молчал. Got calls: {[type(c).__name__ for c in bot.calls]!r}"
        )
        assert len(answer_calls) == 1, (
            "W6: only catchall must answer (top-down first-match в aiogram 3.x — "
            "один handler заматчится). Multiple AnswerCallbackQuery calls suggest "
            f"another handler also matched. Got: {len(answer_calls)} calls"
        )
        assert answer_calls[0].text == "Используйте /cancel для отмены", (
            "W6: stale callback в AdminMoveStates must reach "
            f"admin_state_catchall_callback (hint 'Используйте /cancel'). "
            f"Got: {answer_calls[0].text!r}"
        )

        # Step 4 (regression guard): state preserved — "❌ Отмена" в следующем
        # step всё ещё попадает в admin_cancel_msg (W4) → "Админ-режим отменён".
        # Catchall НЕ чистит state (только callback.answer).
        bot.reset()
        await dp.feed_update(bot, _make_text_update("❌ Отмена", user_id=ADMIN_TG_ID))
        post_texts = _extract_all_send_texts(bot)
        assert any("Админ-режим отменён" in t for t in post_texts), (
            "W6 regression: after stale callback hint, state must still be "
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


# ============================================================
# Session 5.66 E2E — сценарий «дня Екатерины»: полный цикл
# /today (день открыт) → закрыть день → /today снова (баг-состояние)
# → кнопка «Закрыть другой день» жива → календарь → закрыть завтрашний день
# Ловит через real Dispatcher то, что unit-тесты не видят: router wiring,
# callback_data маршрутизацию, FSM-переходы AdminCloseOtherDayStates.
# ============================================================


async def _seed_today_workday_active(
    session_factory: Any,
) -> dict[str, Any]:
    """Seed admin stack + ACTIVE WorkDay на сегодня (10-20 MSK), без броней."""
    from datetime import time as dt_time

    from bot.models import Service

    async with session_factory() as session:
        biz = Business(name="Test", telegram_owner_id=ADMIN_TG_ID, timezone=TZ)
        session.add(biz)
        await session.flush()
        master = Master(business_id=biz.id, name="T", telegram_id=ADMIN_TG_ID, role="owner")
        session.add(master)
        await session.flush()
        client = Client(telegram_id=999888777, name="Client")
        session.add(client)
        await session.flush()

        today = datetime.now(ZoneInfo(TZ)).date()
        wd = WorkDay(
            master_id=master.id,
            work_date=today,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            is_active=True,
            max_concurrent_clients=1,
        )
        session.add(wd)
        await session.flush()

        svc = Service(business_id=biz.id, name="Стрижка", duration_minutes=60, is_active=True)
        session.add(svc)
        await session.commit()
        return {
            "master_id": master.id,
            "business_id": biz.id,
            "client_id": client.id,
            "workday_id": wd.id,
        }


@pytest.mark.asyncio
async def test_e2e_ekaterina_day_cycle_close_today_then_close_other(
    integration_dispatcher: Any,
    session_factory: Any,
) -> None:
    """E2E: после закрытия сегодня кнопка «Закрыть другой день» не пропадает,
    и через неё реально закрывается завтрашний день (Variant B flow жив).

    Сценарий ( буквальный баг-репорт Екатерины ):
    1. /today при открытом пустом дне → обе кнопки, текст «Дата открыта»
    2. Тап [🔒 Закрыть день] → confirm (пустой день → закрывается сразу)
    3. /today снова → НОВЫЙ текст «Сегодня уже закрыт» + кнопка жива
       (до фикса — голый текст, кнопки не было)
    4. Тап [🔒 Закрыть другой день] → календарь
    5. Выбор завтрашней даты в календаре → пустой день закрывается сразу

    freeze_time на весь тест: сид должен работать с той же «сегодняшней»
    датой, что и handler (иначе workday сидится вне frozen-даты → бот
    честно говорит «не открывался» — поймано первым прогоном).
    """
    from bot.models import Service
    from freezegun import freeze_time

    dp, bot = integration_dispatcher

    from datetime import time as dt_time

    with freeze_time("2026-03-17 12:00:00", tz_offset=3):  # Moscow UTC+3
        today = datetime.now(ZoneInfo(TZ)).date()
        tomorrow = today + timedelta(days=1)
        async with session_factory() as session:
            biz = Business(name="Test", telegram_owner_id=ADMIN_TG_ID, timezone=TZ)
            session.add(biz)
            await session.flush()
            master = Master(business_id=biz.id, name="T", telegram_id=ADMIN_TG_ID, role="owner")
            session.add(master)
            await session.flush()
            ctx = {"master_id": master.id, "business_id": biz.id}
            session.add(
                WorkDay(
                    master_id=master.id,
                    work_date=today,
                    start_time=dt_time(10, 0),
                    end_time=dt_time(20, 0),
                    is_active=True,
                    max_concurrent_clients=1,
                )
            )
            session.add(
                WorkDay(
                    master_id=master.id,
                    work_date=tomorrow,
                    start_time=dt_time(10, 0),
                    end_time=dt_time(20, 0),
                    is_active=True,
                    max_concurrent_clients=1,
                )
            )
            session.add(
                Service(business_id=biz.id, name="Стрижка", duration_minutes=60, is_active=True)
            )
            await session.commit()

        # --- Step 1: /today — день открыт, записей нет ---
        await dp.feed_update(bot, _make_text_update("/today"))
        text = _extract_send_text(bot)
        assert "Дата открыта" in text, f"Step 1: ожидали «Дата открыта», got: {text!r}"
        btn_close = await _find_button_by_label(bot, "🔒 Закрыть день")
        btn_other = await _find_button_by_label(bot, "🔒 Закрыть другой день")
        assert btn_close is not None, "Step 1: кнопка «Закрыть день» должна быть"
        assert btn_other is not None, "Step 1: кнопка «Закрыть другой день» должна быть"

        # --- Step 2: тап «Закрыть день» — пустой день закрывается сразу ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(btn_close))
        texts = _extract_all_send_texts(bot)
        assert any("закрыт" in t.lower() for t in texts), (
            f"Step 2: ожидали summary «День ... закрыт», got: {texts!r}"
        )

        # --- Step 3: /today снова — THE BUG-STATE ---
        bot.reset()
        await dp.feed_update(bot, _make_text_update("/today"))
        text = _extract_send_text(bot)
        assert "Сегодня уже закрыт" in text, (
            f"Step 3: до фикса был голый «записей нет» без клавиатуры; "
            f"теперь ожидаем «Сегодня уже закрыт»: {text!r}"
        )
        btn_other = await _find_button_by_label(bot, "🔒 Закрыть другой день")
        assert btn_other is not None, (
            f"Step 3: РЕГРЕССИЯ — кнопка «Закрыть другой день» пропала после "
            f"закрытия дня (исходный баг Session 5.66)! markup: "
            f"{bot.last_reply_markup!r}"
        )
        btn_close = await _find_button_by_label(bot, "🔒 Закрыть день")
        assert btn_close is None, "Step 3: «Закрыть день» должна скрыться"

        # --- Step 4: тап «Закрыть другой день» → календарь ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(btn_other))
        text = _extract_send_text(bot)
        assert "Выберите день" in text, f"Step 4: ожидали календарь, got: {text!r}"

        # --- Step 5: выбор завтрашней даты в SimpleCalendar ---
        # Кнопка дня матчаится по тексту (номер дня) И по callback_data
        # (год/месяц) — иначе «18» из любого другого месяца (если календарь
        # открылся не на март, как в import-time баге) прошёл бы отбор.
        expected_cb = f"simple_calendar:DAY:{tomorrow.year}:{tomorrow.month}:{tomorrow.day}"
        markup = _extract_reply_markup(bot)
        tomorrow_btn = None
        from aiogram.types import InlineKeyboardMarkup

        assert isinstance(markup, InlineKeyboardMarkup)
        for row in markup.inline_keyboard:
            for b in row:
                if b.text.strip() == str(tomorrow.day) and (b.callback_data or "") == expected_cb:
                    tomorrow_btn = b
                    break
            if tomorrow_btn:
                break
        assert tomorrow_btn is not None, (
            f"Step 5: в календаре нет кнопки {expected_cb!r} "
            f"(дата {tomorrow}). Календарь: {markup!r}"
        )

        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(tomorrow_btn))
        # Завтрашний день ПУСТОЙ → закрывается сразу (no confirm step)
        texts = _extract_all_send_texts(bot)
        assert any("закрыт" in t.lower() for t in texts), (
            f"Step 5: ожидали закрытие завтрашнего дня, got: {texts!r}"
        )

        # --- Step 6: финальная сверка БД — оба дня inactive ---
        async with session_factory() as session:
            from sqlalchemy import select

            stmt = select(WorkDay).where(WorkDay.master_id == ctx["master_id"])
            rows = (await session.execute(stmt)).scalars().all()
            active = [r for r in rows if r.is_active]
            assert not active, (
                f"Step 6: оба WorkDay должны быть закрыты, активны: "
                f"{[(r.work_date, r.is_active) for r in rows]}"
            )


@pytest.mark.asyncio
async def test_e2e_client_cancels_own_booking_via_mybookings(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """E2E happy-path УДАЛЕНИЯ записи со стороны клиента:

    /mybookings → список с [❌ Отменить] → тап → запись отменена в БД.

    Verifies:
    - mybookings_msg dispatches (StateFilter(None)) and renders the booking
    - mybookings_keyboard carries MyBookingsCancelCallbackData for THIS booking
    - mybookings_cancel_cb: cancel_booking flips status confirmed→cancelled
      (ownership via client.telegram_id resolve)
    - Bot confirms to the client ('✅ Запись отменена')

    Freeze 2026-08-25 14:00 UTC; booking tomorrow 11:00-12:00 MSK ensures
    `get_client_bookings` upcoming-filter (start_at > now) keeps it visible.
    """
    from decimal import Decimal

    from bot.models import Booking, Client
    from freezegun import freeze_time

    CLIENT_TG = 999_888_771
    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        ctx = await _seed_workday_tomorrow(session_factory)  # workday 2026-08-26 10-12

        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        from datetime import time as dt_time

        async with session_factory() as session:
            client = Client(telegram_id=CLIENT_TG, name="Паша Клиент")
            session.add(client)
            await session.flush()
            start_local = datetime.combine(tomorrow, dt_time(11, 0), tzinfo=ZoneInfo(TZ))
            end_local = datetime.combine(tomorrow, dt_time(12, 0), tzinfo=ZoneInfo(TZ))
            booking = Booking(
                business_id=ctx["business_id"],
                master_id=ctx["master_id"],
                client_id=client.id,
                service_id=ctx["service1_id"],
                service_title_snapshot="Стрижка",
                service_price_snapshot=Decimal("0"),
                client_name_snapshot="Паша Клиент",
                start_at=start_local.astimezone(UTC),
                end_at=end_local.astimezone(UTC),
                status="confirmed",
            )
            session.add(booking)
            await session.commit()
            booking_id = booking.id

        # --- Step 1: /mybookings → список + [❌ Отменить ...] кнопка ---
        await dp.feed_update(bot, _make_text_update("/mybookings", user_id=CLIENT_TG))
        text = _extract_send_text(bot)
        assert "Ваши записи" in text, f"Step 1: ожидали список записей, got: {text!r}"
        assert "Стрижка" in text, f"Step 1: услуга должна быть в списке, got: {text!r}"
        cancel_btn = await _find_button_by_label(bot, "❌ Отменить")
        assert cancel_btn is not None, (
            f"Step 1: кнопка [❌ Отменить] должна быть в mybookings_keyboard, "
            f"markup: {bot.last_reply_markup!r}"
        )

        # --- Step 2: тап [❌ Отменить] → «Запись отменена» ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(cancel_btn, user_id=CLIENT_TG))
        texts = _extract_all_send_texts(bot)
        assert any("Запись отменена" in t for t in texts), (
            f"Step 2: ожидали подтверждение отмены, got: {texts!r}"
        )

        # --- Step 3: DB — статус cancelled ---
        async with session_factory() as session:
            booking_after = await session.scalar(select(Booking).where(Booking.id == booking_id))
        assert booking_after is not None and booking_after.status == "cancelled", (
            f"Step 3: booking.status должен стать cancelled, got: "
            f"{booking_after.status if booking_after else None}"
        )

        # --- Step 4: повторный /mybookings → пустой список (upcoming filter) ---
        bot.reset()
        await dp.feed_update(bot, _make_text_update("/mybookings", user_id=CLIENT_TG))
        text = _extract_send_text(bot)
        assert "нет активных записей" in text.lower(), (
            f"Step 4: после отмены записей быть не должно, got: {text!r}"
        )


@pytest.mark.asyncio
async def test_e2e_admin_close_today_with_active_booking_cancels_it(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """E2E: админ закрывает СЕГОДНЯШНИЙ день с активной записью —
    confirm → день закрыт + запись отменена в БД.

    Дополняет test_e2e_ekaterina_day_cycle... (там оба дня ПУСТЫЕ): здесь
    покрыта ветка active bookings → admin_close_today_confirm_keyboard →
    admin_close_today_confirm_cb.

    1. freeze 06:00 UTC (09:00 MSK — ДО окна 10-12, booking 11:00 ещё upcoming)
    2. _seed_today_with_booking: workday today 10-12 + booking 11:00-12:00
    3. admin /today → [🔒 Закрыть день]
    4. тап → confirm «В этот день 1 запис... Закрыть день и отменить все записи?»
    5. тап [✅ Да, отменить записи] → summary «Отменено записей: 1»
    6. DB: WorkDay inactive + Booking cancelled
    """
    from bot.models import Booking
    from freezegun import freeze_time

    with freeze_time("2026-08-25 06:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        seeded = await _seed_today_with_booking(session_factory)
        booking_id = seeded["booking_id"]

        # --- Step 1: admin /today ---
        await dp.feed_update(bot, _make_text_update("/today"))
        btn_close = await _find_button_by_label(bot, "🔒 Закрыть день")
        assert btn_close is not None, (
            f"Step 1: [🔒 Закрыть день] должна быть (workday active), "
            f"text: {_extract_send_text(bot)!r}"
        )

        # --- Step 2: тап → confirm с перечнем записей ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(btn_close))
        text = _extract_send_text(bot)
        assert "В этот день 1 запис" in text, f"Step 2: ожидали confirm-список, got: {text!r}"
        assert "Закрыть день и отменить все записи?" in text
        confirm_btn = await _find_button_by_label(bot, "✅ Да, отменить записи")
        assert confirm_btn is not None, "Step 2: [✅ Да, отменить записи] должна быть"

        # --- Step 3: тап confirm → summary с числом отменённых ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(confirm_btn))
        texts = _extract_all_send_texts(bot)
        assert any("закрыт" in t.lower() and "Отменено записей: 1" in t for t in texts), (
            f"Step 3: ожидали summary «День закрыт. Отменено записей: 1», got: {texts!r}"
        )
        # notified_count прокидывается в summary админу (а не только в лог).
        assert any("Клиентов уведомлено: 1" in t for t in texts), (
            f"Step 3: summary должен содержать «Клиентов уведомлено: 1», got: {texts!r}"
        )
        # Step 3b: клиенту РЕАЛЬНО ушёл send_message на его chat_id
        # (sent_direct registry —RecordingBot.send_message; до этого ассерта
        # E2E не отличал «уведомлено» от «счётчик сказали»).
        client_tg = seeded["client_telegram_id"]
        direct_to_client = [txt for cid, txt in bot.sent_direct if cid == client_tg]
        assert direct_to_client, (
            f"Step 3b: bot.send_message на chat_id={client_tg} не вызывался "
            f"(sent_direct={bot.sent_direct!r})"
        )
        assert "Ваша запись отменена мастером" in direct_to_client[0], (
            f"Step 3b: текст уведомления клиенту неверен: {direct_to_client[0]!r}"
        )

        # --- Step 4: DB — workday inactive + booking cancelled ---
        async with session_factory() as session:
            master = await session.scalar(select(Master))
            workdays = (
                (await session.execute(select(WorkDay).where(WorkDay.master_id == master.id)))
                .scalars()
                .all()
            )
            assert workdays and all(wd.is_active is False for wd in workdays), (
                f"Step 4: workday должен стать inactive, got: "
                f"{[(wd.work_date, wd.is_active) for wd in workdays]}"
            )
            booking_after = await session.scalar(select(Booking).where(Booking.id == booking_id))
        assert booking_after is not None and booking_after.status == "cancelled", (
            f"Step 4: booking должен стать cancelled, got: {booking_after.status!r}"
        )


@pytest.mark.asyncio
async def test_e2e_admin_move_full_flow_booking_transferred_client_notified(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """P2-A (coverage plan): полный admin_move E2E через integration_dispatcher:
    /today → [🔄 Перенести] → календарь (тап завтра) → слот → [✅ Перенести] →
    booking.status == 'transferred'.

    Flow-level покрытие (router wiring, callback_data, FSM-переходы
    AdminMoveStates.selecting_date → selecting_slot → confirming) поверх
    15 юнитов test_admin_move.py (service-level, без dispatcher).

    Каналы уведомлений (критик NF5 — НЕ смешивать):
    - КЛИЕНТ: handler шлёт через callback.bot.send_message (admin.py:3135)
      → RecordingBot.send_message → bot.sent_direct, chat_id=999888777.
    - МАСТЕР: callback.message.answer → bot.__call__ → bot.calls
      (SendMessage-объекты, «✅ Запись перенесена на ... Клиент уведомлён.»).

    Source booking (workday-only, slot_id=None): ассерт «slot_id изменился»
    из плана неприменим — slot_id stays None (mirror unit-теста
    test_admin_move.py:230); перенос фиксируем через start_at/end_at UTC.

    Сцена: freeze 06:00 UTC (09:00 MSK) — today booking 11:00-12:00
    upcoming; цель — завтра 11:00 (слоты 10:00/10:30/11:00 при
    min_duration 60 от услуги 'Стрижка').
    """
    from datetime import time as dt_time

    from bot.models import Booking, NotificationLog
    from freezegun import freeze_time

    with freeze_time("2026-08-25 06:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        seeded = await _seed_today_with_booking(session_factory)
        booking_id = seeded["booking_id"]
        client_tg = seeded["client_telegram_id"]

        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        # Целевой WorkDay ЗАВТРА 10:00–12:00 (workday-only путь переноса).
        async with session_factory() as session:
            master = await session.scalar(select(Master))
            wd_new = WorkDay(
                master_id=master.id,
                work_date=tomorrow,
                start_time=dt_time(10, 0),
                end_time=dt_time(12, 0),
                is_active=True,
                max_concurrent_clients=1,
            )
            session.add(wd_new)
            await session.commit()

        # --- Step 1: /today → список с [🔄 Перенести] ---
        await dp.feed_update(bot, _make_text_update("/today"))
        today_text = _extract_send_text(bot)
        assert "Записи на сегодня" in today_text, f"Step 1: got: {today_text!r}"
        move_btn = await _find_button_by_label(bot, "🔄")
        assert move_btn is not None, (
            f"Step 1: [🔄 Перенести] должна быть в /today. Got markup: {bot.last_reply_markup!r}"
        )

        # --- Step 2: тап [🔄 Перенести] → календарь (selecting_date) ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(move_btn))
        cal_text = _extract_send_text(bot)
        assert "Выберите новую дату" in cal_text or "Выберите дату" in cal_text, (
            f"Step 2: после тапа [🔄 Перенести] ждём календарь, got: {cal_text!r}"
        )

        # --- Step 3: тап завтра в календаре → слот-пикер (selecting_slot) ---
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow))
        slots_text = _extract_send_text(bot)
        assert "Выберите новое время" in slots_text, (
            f"Step 3: тап дня → слот-пикер, got: {slots_text!r}"
        )
        slot_btn = await _find_button_by_label(bot, "11:00")
        assert slot_btn is not None, (
            f"Step 3: слот 11:00 должен быть в пикере (10:00/10:30/11:00 при "
            f"min_duration 60). Got: {slots_text!r}"
        )

        # --- Step 4: тап слота → summary (confirming) с [✅ Перенести] ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(slot_btn))
        summary_text = _extract_send_text(bot)
        assert "Подтвердите перенос" in summary_text, (
            f"Step 4: слот тап → summary. Got: {summary_text!r}"
        )
        # Locale-агностично: %b зависит от LC_TIME процесса («Aug» / «авг.»).
        # bot/main.py ставит ru_RU.UTF-8 при старте — полный прогон ловит обе.
        assert re.search(r"25 \S+ 2026, 11:00", summary_text), "Step 4: 'Было' = today 11:00 MSK"
        assert re.search(r"26 \S+ 2026, 11:00", summary_text), (
            "Step 4: 'Станет' = tomorrow 11:00 MSK"
        )
        confirm_btn = await _find_button_by_label(bot, "✅ Перенести")
        assert confirm_btn is not None, "Step 4: [✅ Перенести] должна быть"

        # --- Step 5: тап [✅ Перенести] → сервис + уведомления ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(confirm_btn))

        # 5a. КЛИЕНТ уведомлён через bot.send_message → sent_direct (НЕ calls).
        direct_to_client = [txt for cid, txt in bot.sent_direct if cid == client_tg]
        assert direct_to_client, (
            f"Step 5a: клиенту (chat_id={client_tg}) не ушёл send_message. "
            f"sent_direct={bot.sent_direct!r}"
        )
        assert "Ваша запись перенесена мастером" in direct_to_client[0], (
            f"Step 5a: текст клиенту неверен: {direct_to_client[0]!r}"
        )
        assert re.search(r"26 \S+ 2026, 11:00", direct_to_client[0]), (
            f"Step 5a: дата переноса не найдена: {direct_to_client[0]!r}"
        )

        # 5b. МАСТЕР получил summary через message.answer → calls (SendMessage).
        master_texts = _extract_all_send_texts(bot)
        assert any(
            re.search(r"26 \S+ 2026, 11:00", t) and "Клиент уведомлён" in t for t in master_texts
        ), f"Step 5b: master summary не найден в calls. Got: {master_texts!r}"
        # NF5 guard: мастеру НЕ шлется прямой send_message (sent_direct только клиент).
        assert not any(cid == ADMIN_TG_ID for cid, _ in bot.sent_direct), (
            f"Step 5b: master не должен получать send_message. sent_direct={bot.sent_direct!r}"
        )

        # --- Step 6: DB — booking transferred на завтра 11:00 MSK = 08:00 UTC ---
        async with session_factory() as session:
            booking_after = await session.scalar(select(Booking).where(Booking.id == booking_id))
        assert booking_after is not None
        assert booking_after.status == "transferred", (
            f"Step 6: status должен стать transferred, got: {booking_after.status!r}"
        )
        # SQLite хранит naive UTC — нормализуем как в юнитах (test_admin_move.py:217).
        actual_start = booking_after.start_at
        if actual_start.tzinfo is None:
            actual_start = actual_start.replace(tzinfo=UTC)
        actual_end = booking_after.end_at
        if actual_end.tzinfo is None:
            actual_end = actual_end.replace(tzinfo=UTC)
        assert actual_start == datetime(2026, 8, 26, 8, 0, tzinfo=UTC), (
            f"Step 6: start_at = 26 авг 08:00 UTC (11:00 MSK), got: {actual_start!r}"
        )
        assert actual_end == datetime(2026, 8, 26, 9, 0, tzinfo=UTC), (
            f"Step 6: end_at = 26 авг 09:00 UTC (12:00 MSK), got: {actual_end!r}"
        )
        # slot_id stays None — workday-only source (mirror test_admin_move.py:230)
        assert booking_after.slot_id is None

        # --- Step 7: NotificationLog 'client_moved' ровно 1 строка ---
        async with session_factory() as session:
            notif_rows = (
                (
                    await session.execute(
                        select(NotificationLog).where(
                            NotificationLog.booking_id == booking_id,
                            NotificationLog.kind == "client_moved",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(notif_rows) == 1, f"Step 7: клиент_moved лог ровно 1, got: {len(notif_rows)}"


@pytest.mark.asyncio
async def test_e2e_month_boundary_client_books_feb_1st_from_jan_31st(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """P2-B (coverage plan): month-boundary E2E — freeze 31 янв 2026 20:00 MSK,
    клиент /slots → даты записи содержат 1 ФЕВРАЛЯ → тап → слот-пикер показывает
    слоты на 1 фев. Ловит реальный переход месяца в клиентском booking flow.

    NB (давиация от плана — план писался до сверки кода):
    1. «/book → календарь»: с BB-110 /book и /slots НЕ рендерят SimpleCalendar —
       это flat list дат (date_picker_keyboard, callback 'book_date:YYYY-MM-DD',
       client.py:491-493). Визуального «февраля» не существует; month boundary
       ловим по КОНКРЕТНОЙ дате 2026-02-01 в списке и в callback_data.
       Клиентский SimpleCalendar жив только в /transfer (BB-110 scope).
    2. freeze из плана «20:00 tz_offset=3» даёт freezegun-ловушку: строка
       трактуется как UTC+3 → frozen 23:00 UTC = 02:00 MSK 1 ФЕВРАЛЯ (месяц уже
       сменился — verified). Честная заморозка 20:00 MSK 31 янв = 17:00 UTC
       tz_offset=0 (паттерн всех integration-тестов).

    Import-time календарный фикс f45ee2b — про admin_calendar_keyboard
    (month на момент вызова), покрыт своими E2E; клиентский /slots здесь
    проверяет границу месяца в date-фильтрах (get_bookable_dates:409-422,
    book_date_cb → слот-пикер).
    """
    from freezegun import freeze_time

    # 17:00 UTC = 20:00 MSK суббота 31 янв 2026. Завтра = ВС 1 ФЕВРАЛЯ.
    with freeze_time("2026-01-31 17:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        # WorkDay ЗАВТРА (1 фев) 10:00–12:00 + услуга «Экспресс 30» (30 мин).
        await _seed_workday_tomorrow_tz_edge(
            session_factory, start_time_str="10:00", end_time_str="12:00"
        )

        client_tg = 999_888_777

        # --- Step 1: /slots → flat list дат, среди них 1 ФЕВРАЛЯ ---
        await dp.feed_update(bot, _make_text_update("/slots", user_id=client_tg))
        step1 = _extract_send_text(bot)
        assert "Выберите дату" in step1, f"Step 1: /slots → date picker, got: {step1!r}"

        markup = _extract_reply_markup(bot)
        assert isinstance(markup, InlineKeyboardMarkup)
        feb_1_btn: InlineKeyboardButton | None = None
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == "book_date:2026-02-01":
                    feb_1_btn = btn
                    break
            if feb_1_btn is not None:
                break
        assert feb_1_btn is not None, (
            f"Step 1: кнопка 1 февраля (book_date:2026-02-01) должна быть в "
            f"списке. markup: {markup.inline_keyboard!r}"
        )
        # Label содержит дату февраля в формате %d.%m (не январскую старую).
        assert "01.02" in feb_1_btn.text, (
            f"Step 1: label кнопки 1 фев должен содержать '01.02', got: {feb_1_btn.text!r}"
        )

        # --- Step 2: тап 1 февраля → service picker (WorkDay сидирован) ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(feb_1_btn, user_id=client_tg))
        step2 = _extract_send_text(bot)
        assert "Выберите услугу" in step2, (
            f"Step 2: тап 1 фев → service picker (workday есть, не «мастер не "
            f"работает»). got: {step2!r}"
        )
        svc_btn = await _find_button_by_label(bot, "Экспресс 30")
        assert svc_btn is not None, "Step 2: услуга «Экспресс 30» в пикере"

        # --- Step 3: тап услуги → слот-пикер с PACLотами НА 1 ФЕВ ---
        bot.reset()
        await dp.feed_update(bot, _make_callback_update_from_button(svc_btn, user_id=client_tg))
        step3 = _extract_send_text(bot)
        assert "Выберите время" in step3, f"Step 3: услуга → слот-пикер, got: {step3!r}"
        slot_btn = await _find_button_by_label(bot, "10:00")
        assert slot_btn is not None, (
            f"Step 3: слот 10:00 на 1 фев должен быть в пикере. got: {step3!r}"
        )


@pytest.mark.asyncio
async def test_e2e_second_client_does_not_see_occupied_slot(
    integration_dispatcher: tuple[Dispatcher, MagicMock],
    session_factory: Any,
) -> None:
    """E2E граничный: занятый слот НЕ показывается второму клиенту.

    Slot picker строится из реальных Booking в БД: после записи первого
    клиента (завтра 10:00-11:00, Стрижка 60 мин) второй клиент в том же
    окне не должен видеть слот 10:00 — только 11:00.

    1. seed workday tomorrow 10-12 + booking 10:00-11:00 (client_1)
    2. client_2: /slots → календарь → завтра → сервис-пикер
    3. тап Стрижка (60 мин) → слот-пикер
    4. слота 10:00 НЕТ, слот 11:00 ЕСТЬ
    """
    from decimal import Decimal

    from bot.models import Booking, Client
    from freezegun import freeze_time

    CLIENT1_TG = 111_111_111
    CLIENT2_TG = 999_888_772
    with freeze_time("2026-08-25 14:00:00", tz_offset=0):
        dp, bot = integration_dispatcher
        ctx = await _seed_workday_tomorrow(session_factory)

        tomorrow = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
        from datetime import time as dt_time

        async with session_factory() as session:
            client1 = Client(telegram_id=CLIENT1_TG, name="Первый Клиент")
            session.add(client1)
            await session.flush()
            start_local = datetime.combine(tomorrow, dt_time(10, 0), tzinfo=ZoneInfo(TZ))
            end_local = datetime.combine(tomorrow, dt_time(11, 0), tzinfo=ZoneInfo(TZ))
            session.add(
                Booking(
                    business_id=ctx["business_id"],
                    master_id=ctx["master_id"],
                    client_id=client1.id,
                    service_id=ctx["service1_id"],
                    service_title_snapshot="Стрижка",
                    service_price_snapshot=Decimal("0"),
                    client_name_snapshot="Первый Клиент",
                    start_at=start_local.astimezone(UTC),
                    end_at=end_local.astimezone(UTC),
                    status="confirmed",
                )
            )
            await session.commit()

        # --- Step 1: client_2 /slots → календарь ---
        await dp.feed_update(bot, _make_text_update("/slots", user_id=CLIENT2_TG))
        assert "Выберите дату" in _extract_send_text(bot)

        # --- Step 2: выбор завтрашнего дня → сервис-пикер ---
        bot.reset()
        await dp.feed_update(bot, _make_calendar_day_update(tomorrow, user_id=CLIENT2_TG))
        assert "Выберите услугу" in _extract_send_text(bot)
        haircut_btn = await _find_button_by_label(bot, "Стрижка")
        assert haircut_btn is not None, "Step 2: кнопка Стрижка должна быть в пикере"

        # --- Step 3: тап Стрижка (60 мин) → слот-пикер ---
        bot.reset()
        await dp.feed_update(
            bot, _make_callback_update_from_button(haircut_btn, user_id=CLIENT2_TG)
        )
        assert "Выберите время" in _extract_send_text(bot)

        # --- Step 4: занятый 10:00 скрыт, свободный 11:00 виден ---
        occupied_btn = await _find_button_by_label(bot, "10:00")
        assert occupied_btn is None, (
            f"Step 4: слот 10:00 занят booking'ом client_1 — не должен "
            f"предлагаться. markup: {bot.last_reply_markup!r}"
        )
        free_btn = await _find_button_by_label(bot, "11:00")
        assert free_btn is not None, "Step 4: свободный слот 11:00 должен быть виден"
