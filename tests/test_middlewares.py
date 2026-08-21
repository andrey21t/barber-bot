"""Tests for bot.middlewares.session_timeout — FSM timeout 30 min от последнего msg.

Coverage (AUTONOMOUS_COVERAGE_PROMPT.md T2):
- state is None → pass to handler (no-op)
- last_at_str отсутствует → first call, extend timer, pass
- last_at_str невалидный ISO → treat as None, extend timer
- elapsed < TTL → extend timer, pass
- elapsed > TTL → state.clear() BEFORE event.answer → return None (handler NOT called)
- Race ordering: state.clear called BEFORE event.answer (MY-VIBE-RULES.md 24)
- freeze_time для детерминированного elapsed

Pattern: SessionTimeoutMiddleware().__call__(handler, event, data) + AsyncMock state.
The middleware reads state from data['state'], calls state.get_data/update_data/clear,
and either passes to handler or returns None.

Race condition (MY-VIBE-RULES.md 24):
  state.clear() must happen BEFORE event.answer — otherwise user can tap a button
  between answer and clear, causing the answer to land on the cleared state.
  We verify call order via AsyncMock.call_args_list.index().
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.middlewares.session_timeout import SESSION_TTL_SEC, SessionTimeoutMiddleware
from freezegun import freeze_time

# ============================================================
# Helpers — mock state, event, handler
# ============================================================


def _make_state(
    *,
    last_message_at: str | None = None,
) -> AsyncMock:
    """Mock FSMContext — get_data returns dict with last_message_at.

    Methods used by middleware: get_data (async), update_data (async), clear (async).
    """
    state = AsyncMock()
    state_data = {"last_message_at": last_message_at} if last_message_at else {}
    state.get_data = AsyncMock(return_value=state_data)
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    return state


def _make_event(*, has_answer: bool = True) -> MagicMock:
    """Mock aiogram TelegramObject. `answer` is AsyncMock if has_answer else missing."""
    event = MagicMock()
    if has_answer:
        event.answer = AsyncMock()
    else:
        # Simulate event without .answer (e.g. a raw Update without answer method)
        if hasattr(event, "answer"):
            del event.answer
    return event


def _make_handler() -> AsyncMock:
    """Mock handler — middleware passes (event, data) when state ok."""
    return AsyncMock(return_value="handler-result")


# ============================================================
# 1. state is None → pass to handler (no-op, no state calls)
# ============================================================


@pytest.mark.asyncio
async def test_state_none_passes_to_handler_no_state_calls() -> None:
    """data['state'] missing → middleware just calls handler, never touches state.

    This is the path for non-FSM updates (e.g. /start without state).
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()
    data: dict[str, Any] = {}  # no 'state' key

    result = await mw(handler, event, data)

    assert result == "handler-result"
    handler.assert_awaited_once_with(event, data)


# ============================================================
# 2. last_at_str отсутствует → first call, extend timer, pass
# ============================================================


@pytest.mark.asyncio
async def test_first_call_no_last_at_extends_timer_and_passes() -> None:
    """State has no 'last_message_at' key → first interaction.

    Middleware should: NOT clear, call update_data (extend), call handler.
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()
    state = _make_state(last_message_at=None)
    data = {"state": state}

    result = await mw(handler, event, data)

    assert result == "handler-result"
    state.clear.assert_not_awaited()
    state.update_data.assert_awaited_once()
    handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_first_call_update_data_records_current_iso_timestamp() -> None:
    """update_data must be called with a valid ISO string (JSON-serializable).

    Use freeze_time to assert the exact timestamp passed.
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()
    state = _make_state(last_message_at=None)
    data = {"state": state}

    with freeze_time("2026-03-17 12:00:00", tz_offset=0):
        await mw(handler, event, data)

    state.update_data.assert_awaited_once()
    call_kwargs = state.update_data.await_args.kwargs
    assert "last_message_at" in call_kwargs
    # ISO string parseable, UTC
    parsed = datetime.fromisoformat(call_kwargs["last_message_at"])
    assert parsed == datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)


# ============================================================
# 3. last_at_str невалидный ISO → treat as None, extend timer
# ============================================================


@pytest.mark.asyncio
async def test_invalid_iso_last_at_treated_as_none_extends_and_passes() -> None:
    """last_at_str='not-a-date' → fromisoformat raises ValueError → last_at=None.

    Middleware should NOT clear (treats as first call), extend timer, pass to handler.
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()
    state = _make_state(last_message_at="not-a-date")
    data = {"state": state}

    result = await mw(handler, event, data)

    assert result == "handler-result"
    state.clear.assert_not_awaited()
    state.update_data.assert_awaited_once()
    handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_invalid_iso_none_value_treated_as_none() -> None:
    """Edge: last_at_str is non-string (None) → fromisoformat raises TypeError →
    caught by (ValueError, TypeError), last_at=None, extend + pass.

    Note: get_data returns {} when key absent — but if state stores `None` explicitly
    (e.g. legacy bug), middleware should still handle gracefully.
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()
    # Simulate state where last_message_at is explicitly None (not absent)
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"last_message_at": None})
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    data = {"state": state}

    result = await mw(handler, event, data)

    assert result == "handler-result"
    state.clear.assert_not_awaited()
    state.update_data.assert_awaited_once()


# ============================================================
# 4. elapsed < TTL → extend timer, pass
# ============================================================


@pytest.mark.asyncio
async def test_elapsed_under_ttl_extends_timer_and_passes() -> None:
    """last_message_at = now - 5 min, TTL=30 min → elapsed=300s < 1800s → extend + pass."""
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()

    # 5 minutes ago (well under 30-min TTL)
    recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    state = _make_state(last_message_at=recent)
    data = {"state": state}

    result = await mw(handler, event, data)

    assert result == "handler-result"
    state.clear.assert_not_awaited()
    state.update_data.assert_awaited_once()
    handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_elapsed_exactly_at_ttl_does_not_clear_strict_inequality() -> None:
    """Boundary: elapsed == TTL → middleware uses strict `>` → NOT timed out, extend + pass.

    SESSION_TTL_SEC=1800. last_at = now - 1800s exactly.
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()

    # Exactly TTL ago — frozen time makes elapsed deterministic
    with freeze_time("2026-03-17 12:00:00", tz_offset=0):
        ttl_ago = (datetime.now(UTC) - timedelta(seconds=SESSION_TTL_SEC)).isoformat()
        state = _make_state(last_message_at=ttl_ago)
        data = {"state": state}

        result = await mw(handler, event, data)

    # Strict `>` → elapsed == TTL does NOT trigger timeout
    assert result == "handler-result"
    state.clear.assert_not_awaited()
    state.update_data.assert_awaited_once()


# ============================================================
# 5. elapsed > TTL → state.clear() BEFORE event.answer → return None (handler NOT called)
# ============================================================


@pytest.mark.asyncio
async def test_elapsed_over_ttl_clears_state_answers_and_returns_none() -> None:
    """last_at = now - 60 min, TTL=30 min → elapsed=3600s > 1800s → timeout.

    Middleware should: state.clear(), event.answer("⏰ Сессия истекла..."), return None.
    Handler must NOT be called.
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()

    # 60 minutes ago (over TTL)
    old = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    state = _make_state(last_message_at=old)
    data = {"state": state}

    with freeze_time("2026-03-17 12:00:00", tz_offset=0):
        # Fix 'now' so elapsed is exactly 3600s
        old_fixed = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
        state.get_data = AsyncMock(return_value={"last_message_at": old_fixed})

        result = await mw(handler, event, data)

    assert result is None
    state.clear.assert_awaited_once()
    event.answer.assert_awaited_once()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_ttl_answer_text_mentions_timeout_and_book_command() -> None:
    """event.answer text contains ⏰ emoji + 'Сессия истекла' + '/book' hint."""
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()

    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    state = _make_state(last_message_at=old)
    data = {"state": state}

    result = await mw(handler, event, data)

    assert result is None
    args, _ = event.answer.await_args
    text = args[0]
    assert "⏰" in text
    assert "Сессия истекла" in text
    assert "/book" in text


# ============================================================
# 6. Race ordering — state.clear() BEFORE event.answer
# ============================================================


@pytest.mark.asyncio
async def test_race_ordering_state_clear_before_event_answer() -> None:
    """MY-VIBE-RULES.md 24 — state.clear() must happen BEFORE event.answer.

    Why: between answer and clear, user can tap an inline button. Answer lands
    on cleared state → undefined behavior (e.g. KeyError on state data lookup).

    We verify via AsyncMock mock_call ordering — for state (AsyncMock) and
    event (separate AsyncMock with .answer), we collect (mock_name, call_index)
    pairs in chronological order using each mock's .await_count at the moment
    of each call. Simpler: assert state.clear.await_count == 1 BEFORE event.answer
    by using a shared ordered call log via side_effect wrappers.
    """
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event()

    # 60 min ago (over TTL)
    old = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    state = _make_state(last_message_at=old)
    data = {"state": state}

    # Shared ordered log — appended by side_effect of clear and answer
    call_log: list[str] = []

    async def _clear_side_effect() -> None:
        call_log.append("state.clear")

    async def _answer_side_effect(*args: Any, **kwargs: Any) -> None:
        call_log.append("event.answer")

    state.clear.side_effect = _clear_side_effect
    event.answer.side_effect = _answer_side_effect

    result = await mw(handler, event, data)

    assert result is None
    # Both called exactly once
    assert state.clear.await_count == 1
    assert event.answer.await_count == 1
    # Order: clear BEFORE answer
    assert call_log == ["state.clear", "event.answer"], (
        "state.clear() must be called BEFORE event.answer() — race condition defense "
        "(MY-VIBE-RULES.md 24). User could tap inline button between answer and clear."
    )


# ============================================================
# 7. event without .answer — hasattr check
# ============================================================


@pytest.mark.asyncio
async def test_over_ttl_event_without_answer_clears_state_only() -> None:
    """Edge: event has no .answer method (e.g. a raw Update without answer) →
    middleware clears state but skips event.answer. Handler NOT called."""
    mw = SessionTimeoutMiddleware()
    handler = _make_handler()
    event = _make_event(has_answer=False)  # event without .answer

    # 60 min ago (over TTL)
    old = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    state = _make_state(last_message_at=old)
    data = {"state": state}

    result = await mw(handler, event, data)

    assert result is None
    state.clear.assert_awaited_once()
    handler.assert_not_awaited()
    # event.answer does not exist — no assertion needed (no AttributeError raised)


# ============================================================
# 8. TTL constant sanity
# ============================================================


def test_session_ttl_sec_is_1800_seconds_30_minutes() -> None:
    """Lock the constant — change here would break user expectation of 30-min timeout."""
    assert SESSION_TTL_SEC == 1800
