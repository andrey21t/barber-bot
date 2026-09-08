"""Tests for bot.handlers.start — /start command with admin/client branch.

Coverage (AUTONOMOUS_COVERAGE_PROMPT.md T6):
- /start from admin → welcome message + admin_keyboard
- /start from client → client welcome message ('Запишитесь командой /book')

Pattern: direct handler invocation with mock Message + User. No DB, no FSM.
Gates: qa-verify-and-fix only (trivial, deep-analysis + code-review skipped per
AUTONOMOUS_COVERAGE_PROMPT.md T6).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message, User
from bot.config import get_settings
from bot.handlers import start as start_handlers

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


# ============================================================
# /start — admin branch
# ============================================================


@pytest.mark.asyncio
async def test_cmd_start_admin_shows_welcome_and_inline_menu() -> None:
    """/start from admin (ADMIN_ID) → cleanup reply keyboard + inline menu.

    Этап 3 (Session 5.9): cmd_start показывает admin_inline_menu() (5 inline
    кнопок) вместо admin_keyboard() (reply keyboard). Текст больше НЕ
    перечисляет /addslots /closeslot /today /week /services — только welcome.

    Fix (post-deploy): admin-ветка отправляет 2 сообщения — сначала cleanup
    (ReplyKeyboardRemove), потом inline menu. Telegram не убирает старую
    reply keyboard автоматически, нужен явный ReplyKeyboardRemove.
    """
    msg = _make_message(user_id=ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    # 2 вызова answer: (1) cleanup reply keyboard, (2) inline menu
    assert msg.answer.await_count == 2
    state.clear.assert_awaited_once()  # W1 fix: /start clears FSM state

    # Второй вызов — inline menu
    second_call = msg.answer.await_args_list[1]
    text = str(second_call.args[0]) if second_call.args else str(second_call.kwargs.get("text", ""))
    assert "Привет, Екатерина" in text
    # Этап 3: текст больше НЕ перечисляет команды (только welcome)
    assert "/addslots" not in text
    assert "/closeslot" not in text
    reply_markup = second_call.kwargs.get("reply_markup")
    assert reply_markup is not None, "admin /start must include inline menu"
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove

    assert isinstance(reply_markup, InlineKeyboardMarkup), "must be inline keyboard"

    # Первый вызов — cleanup reply keyboard
    first_call = msg.answer.await_args_list[0]
    first_rm = first_call.kwargs.get("reply_markup")
    assert isinstance(first_rm, ReplyKeyboardRemove), (
        "first message must cleanup old reply keyboard"
    )


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
