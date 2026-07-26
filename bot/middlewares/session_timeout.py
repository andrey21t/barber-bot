"""SessionTimeoutMiddleware — FSM timeout 30 минут от ПОСЛЕДНЕГО сообщения.

Spec.md 497-528 + MY-VIBE-RULES.md (FSM timeout точка отсчёта):
- timeout = (now - last_message_at) > SESSION_TTL_SEC (30 минут)
- state.clear() ДО event.answer (race condition) — иначе тапнет кнопку между answer и clear
- update_data(last_message_at) ПОСЛЕ timeout check, ДО handler — продлевает таймер
- datetime.now(UTC) (НЕ datetime.utcnow() — deprecated в Python 3.12, упадёт в ruff UP017)
- ISO format string в FSM storage (JSON-serializable для MemoryStorage/PostgresStorage/RedisStorage)
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

SESSION_TTL_SEC = 1800  # 30 минут


class SessionTimeoutMiddleware(BaseMiddleware):
    """Clears FSM state if user has been inactive > SESSION_TTL_SEC.

    Pattern (MY-VIBE-RULES.md 17-26):
        1. Read last_message_at from state data
        2. If elapsed > TTL → state.clear() BEFORE event.answer → return
        3. Otherwise update last_message_at to now() → extend timer
        4. Pass to handler
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if state is None:
            return await handler(event, data)

        state_data = await state.get_data()
        last_at_str = state_data.get("last_message_at")
        if last_at_str:
            try:
                last_at = datetime.fromisoformat(last_at_str)
            except (ValueError, TypeError):
                last_at = None

            if last_at is not None:
                elapsed = (datetime.now(UTC) - last_at).total_seconds()
                if elapsed > SESSION_TTL_SEC:
                    # state.clear() BEFORE event.answer (race condition, MY-VIBE-RULES.md 24)
                    await state.clear()
                    if hasattr(event, "answer"):
                        await event.answer(
                            "⏰ Сессия истекла (30 мин бездействия). "
                            "Начните заново через /book"
                        )
                    return None  # handler NOT called

        # Extend timer — update last_message_at to now() (BEFORE handler)
        await state.update_data(last_message_at=datetime.now(UTC).isoformat())
        return await handler(event, data)
