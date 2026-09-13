"""Tests for bot.handlers.start — /start command with admin/client branch.

Coverage (AUTONOMOUS_COVERAGE_PROMPT.md T6):
- /start from admin → welcome message + admin_keyboard (master.name from DB lookup)
- /start from client → client welcome message ('Запишитесь командой /book')

Pattern: direct handler invocation with mock Message + User. No FSM.
DB lookup via async_session_factory patched to in-memory SQLite (mirrors
test_admin_handlers.py:74 patched_session_factory pattern).

Gates: qa-verify-and-fix only (trivial, deep-analysis + code-review skipped per
AUTONOMOUS_COVERAGE_PROMPT.md T6).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message, User
from bot.config import get_settings
from bot.handlers import start as start_handlers
from bot.models import Business, Master

ADMIN_TG_ID: int = get_settings().ADMIN_ID
NON_ADMIN_TG_ID: int = 999111222


def _make_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_message(user_id: int) -> MagicMock:
    """Mock aiogram.Message — answer is AsyncMock for assertion."""
    msg = MagicMock(spec=Message)
    msg.from_user = _make_user(user_id)
    msg.answer = AsyncMock()
    return msg


def _make_state() -> MagicMock:
    """Mock FSMContext — clear() is AsyncMock (cmd_start calls state.clear()).

    Этап 3 fix W1 (code-review 5.9): cmd_start now clears state at entry.
    """
    state = MagicMock()
    state.clear = AsyncMock()
    return state


def _answer_text(msg: MagicMock) -> str:
    args = msg.answer.call_args.args
    return str(args[0]) if args else str(msg.answer.call_args.kwargs.get("text", ""))


@pytest.fixture
def patched_session_factory(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: Any,
) -> Any:
    """Replace `bot.handlers.start.async_session_factory` with the test engine's
    session_factory so DB lookups in /start hit in-memory SQLite.

    Mirrors test_admin_handlers.py:66 patched_session_factory pattern.
    """
    monkeypatch.setattr(start_handlers, "async_session_factory", session_factory)
    return session_factory


async def _seed_master(
    session_factory: Any,
    *,
    name: str,
    telegram_id: int = ADMIN_TG_ID,
) -> None:
    """Seed a Master row into the test DB for /start DB lookup."""
    async with session_factory() as session:
        biz = Business(
            name="TestSalon",
            telegram_owner_id=telegram_id,
            timezone="Europe/Moscow",
        )
        session.add(biz)
        await session.flush()
        master = Master(
            business_id=biz.id,
            name=name,
            telegram_id=telegram_id,
            role="owner",
        )
        session.add(master)
        await session.commit()


# ============================================================
# /start — admin branch
# ============================================================


@pytest.mark.asyncio
async def test_cmd_start_admin_shows_welcome_and_reply_keyboard(
    patched_session_factory: Any,
    session_factory: Any,
) -> None:
    """/start from admin (ADMIN_ID) → welcome + always-on reply keyboard (3 кнопки).

    master.name берётся из DB lookup (Session 2026-09-13: замена hardcoded
    "Екатерина" на универсальный DB lookup с fallback "мастер"). Задел для
    multi-tenant: при добавлении второго мастера greeting подхватится
    автоматически по telegram_id.

    Session 5.62 (пункт 5): admin-ветка переведена с inline menu на always-on
    reply keyboard (📋 Меню / 📅 Сегодня / 🗓 Неделя). Решает жалобу "inline
    menu уезжает вверх, нужно скроллить или /menu".
    """
    from aiogram.types import ReplyKeyboardMarkup

    await _seed_master(session_factory, name="Екатерина")
    msg = _make_message(user_id=ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    # 1 вызов answer (раньше было 2 — cleanup + inline menu, теперь 1 — welcome + reply keyboard)
    msg.answer.assert_awaited_once()
    state.clear.assert_awaited_once()  # W1 fix: /start clears FSM state

    text = _answer_text(msg)
    # master.name подхвачен из DB (НЕ hardcoded)
    assert "Привет, Екатерина" in text, "greeting must use master.name from DB"
    # 5.62: текст ссылается на reply keyboard кнопку "Меню"
    assert "Меню" in text, "5.62: admin /start must reference reply keyboard button"
    reply_markup = msg.answer.call_args.kwargs.get("reply_markup")
    assert reply_markup is not None, "admin /start must include reply keyboard"
    assert isinstance(reply_markup, ReplyKeyboardMarkup), (
        "5.62: admin /start must use reply keyboard (always-on, not inline)"
    )
    # Reply keyboard has 3 buttons: 📋 Меню / 📅 Сегодня / 🗓 Неделя
    flat_buttons = [btn for row in reply_markup.keyboard for btn in row]
    button_texts = {btn.text for btn in flat_buttons}
    assert "📋 Меню" in button_texts
    assert "📅 Сегодня" in button_texts
    assert "🗓 Неделя" in button_texts
    assert reply_markup.is_persistent, "5.62: reply keyboard must be always-on (is_persistent=True)"


@pytest.mark.asyncio
async def test_cmd_start_admin_fallback_when_master_not_found(
    patched_session_factory: Any,
) -> None:
    """DB lookup возвращает None (мастер не найден — новый сервер, миграция не
    накачена) → fallback "мастер". НЕ падает, НЕ None.name AttributeError.

    Edge case для multi-tenant: новый мастер может тапнуть /start до того, как
    админ создаст его запись в БД. Бот должен вежливо поздороваться, а не
    упасть с 500.
    """
    msg = _make_message(user_id=ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Привет, мастер! 👋" in text, "fallback to 'мастер' when master not found in DB"
    assert "Екатерина" not in text, "must NOT hardcode name when DB lookup misses"


@pytest.mark.asyncio
async def test_cmd_start_admin_fallback_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB session fail (Postgres down) → fallback "мастер", /start НЕ падает.

    Defense-in-depth: greeting — это cosmetic, не должно блокировать доступ к
    боту. Если Postgres упал, мастер всё ещё может тапнуть "📋 Меню" и работать
    (другие handlers вернут ошибки, но /start не должен 500'ить).
    """

    async def _raising_session_factory() -> Any:
        raise RuntimeError("Postgres down")

    # async_session_factory() as session: — контекстный менеджер. Мокаем чтобы
    # __aenter__ поднимал RuntimeError.
    class _RaisingCtx:
        async def __aenter__(self) -> Any:
            raise RuntimeError("Postgres down")

        async def __aexit__(self, *args: object) -> None:
            pass

    def _factory() -> _RaisingCtx:
        return _RaisingCtx()

    monkeypatch.setattr(start_handlers, "async_session_factory", _factory)
    msg = _make_message(user_id=ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Привет, мастер! 👋" in text, "DB error must fallback to 'мастер', not crash /start"


@pytest.mark.asyncio
async def test_cmd_start_admin_escapes_html_in_master_name(
    patched_session_factory: Any,
    session_factory: Any,
) -> None:
    """master.name с HTML спецсимволами (<, >, &) → html.escape в greeting.

    Defense-in-depth: master.name в БД может содержать "<script>" (через
    /addmaster или прямой INSERT). Без escape → TelegramBadRequest → /start
    падает. Mirrors scheduler.py:189 pattern.
    """
    await _seed_master(session_factory, name="<script>alert('xss')</script>")
    msg = _make_message(user_id=ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    # HTML tags escaped — raw <script> НЕ должен попасть в Telegram message
    assert "&lt;script&gt;" in text, "master.name must be HTML-escaped"
    assert "<script>" not in text, "raw HTML in greeting → TelegramBadRequest"


@pytest.mark.asyncio
async def test_cmd_start_admin_fallback_when_name_empty(
    patched_session_factory: Any,
    session_factory: Any,
) -> None:
    """master.name = "" (empty string или whitespace) → fallback "мастер".

    Edge case: хотя models.py:57 не nullable, DB seed может записать "" через
    прямой INSERT или старую миграцию. .strip() check предотвращает "Привет, !".
    """
    await _seed_master(session_factory, name="   ")
    msg = _make_message(user_id=ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Привет, мастер! 👋" in text, "whitespace-only name must fallback to 'мастер'"
    assert "Привет,    " not in text, "must NOT render whitespace-only name"


# ============================================================
# /start — client branch
# ============================================================


@pytest.mark.asyncio
async def test_cmd_start_client_shows_booking_hint() -> None:
    """/start from non-admin → welcome text + reply keyboard (Session 5.36 / B.13).

    B.13: client /start moved from inline single-button menu to a 2-button
    ReplyKeyboardMarkup (💇 Записаться / 📋 Мои записи) — always-on buttons at
    the bottom of the chat. Inline-keyboard booking flow (calendar, slots,
    confirm) is unchanged — reply keyboard is hidden during booking
    (ReplyKeyboardRemove in slot_cb/slot_30_cb) and restored after /cancel
    or confirm_cb.
    """
    from aiogram.types import ReplyKeyboardMarkup

    msg = _make_message(user_id=NON_ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    msg.answer.assert_awaited_once()
    state.clear.assert_awaited_once()  # W1 fix: /start clears FSM state (any branch)
    text = _answer_text(msg)
    assert "Привет" in text
    assert "бот для записи к парикмахеру" in text
    # B.13: hint text changed — "Кнопки внизу — записывайтесь или смотрите свои записи:"
    assert "Кнопки внизу" in text, "B.13: client /start must reference the reply keyboard"
    assert "/book" not in text, "client /start must NOT show /book text hint — use reply keyboard"
    reply_markup = msg.answer.call_args.kwargs.get("reply_markup")
    assert reply_markup is not None, "client /start must include reply keyboard"
    assert isinstance(reply_markup, ReplyKeyboardMarkup), (
        "B.13: must be reply keyboard (not inline)"
    )
    # Reply keyboard has 2 buttons: 💇 Записаться / 📋 Мои записи
    flat_buttons = [btn for row in reply_markup.keyboard for btn in row]
    assert len(flat_buttons) >= 1
    assert any("Записаться" in btn.text for btn in flat_buttons)


@pytest.mark.asyncio
async def test_cmd_start_client_shows_reply_keyboard_with_2_buttons() -> None:
    """B.13: client /start reply keyboard has EXACTLY 2 buttons matching
    CLIENT_REPLY_BOOK_LABEL / CLIENT_REPLY_MYBOOKINGS_LABEL constants.

    Defense-in-depth: handlers (reply_book_msg / reply_mybookings_msg) match
    on F.text == these constants, so the keyboard must produce the exact same
    strings — otherwise the buttons would be silent (no handler match).
    """
    from aiogram.types import ReplyKeyboardMarkup
    from bot.keyboards.client import (
        CLIENT_REPLY_BOOK_LABEL,
        CLIENT_REPLY_MYBOOKINGS_LABEL,
    )

    msg = _make_message(user_id=NON_ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    reply_markup = msg.answer.call_args.kwargs.get("reply_markup")
    assert isinstance(reply_markup, ReplyKeyboardMarkup), "must be reply keyboard"
    # Flatten the 2D keyboard grid and assert exactly 2 buttons.
    flat_buttons = [btn for row in reply_markup.keyboard for btn in row]
    assert len(flat_buttons) == 2, f"expected 2 reply buttons, got {len(flat_buttons)}"
    button_texts = {btn.text for btn in flat_buttons}
    assert button_texts == {CLIENT_REPLY_BOOK_LABEL, CLIENT_REPLY_MYBOOKINGS_LABEL}, (
        f"reply buttons must match handler F.text constants, got {button_texts}"
    )
