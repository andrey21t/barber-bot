"""Unit-тесты _notify_cancelled_clients — цикл уведомлений при закрытии дня.

Прямые вызовы функции (без Dispatcher): AsyncMock bot со side_effect
сценариями + monkeypatched async_session_factory (паттерн как в
tests/test_integration_admin_flows.py:84).

Классы багов (критик pass 1-2, F3-F5):
- flood control: TelegramRetryAfter → sleep(retry_after) + retry once;
- blocked bot: aiogram кидает TelegramForbiddenError (403), НЕ
  TelegramBadRequest — до фикса цикл падал на первом заблокировавшем клиенте,
  остальные не уведомлялись (день при этом уже закрыт);
- mixed loop: 1 заблокировал + 1 живой → цикл не рвётся, notified_count=1.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from bot.handlers.admin import _notify_cancelled_clients
from bot.models import Booking, Business, Client, Master, Service
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed_stack(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client_tg_ids: list[int],
) -> tuple[list[Booking], str]:
    """Business/master/service + N clients + N cancelled bookings (11:00 MSK).

    Returns (bookings, business_tz). Bookings passed to the function as a list —
    order matters for side_effect scenarios (list order == notify order).
    """

    async with session_factory() as session:
        biz = Business(name="Test", telegram_owner_id=461355056, timezone="Europe/Moscow")
        session.add(biz)
        await session.flush()
        master = Master(business_id=biz.id, name="T", telegram_id=461355056, role="owner")
        session.add(master)
        await session.flush()
        svc = Service(business_id=biz.id, name="Стрижка", duration_minutes=60,
                      price=Decimal("0"), is_active=True)
        session.add(svc)
        await session.flush()

        bookings: list[Booking] = []
        for tg_id in client_tg_ids:
            client = Client(telegram_id=tg_id, name=f"c_{tg_id}")
            session.add(client)
            await session.flush()
            booking = Booking(
                business_id=biz.id,
                master_id=master.id,
                client_id=client.id,
                service_id=svc.id,
                service_title_snapshot="Стрижка",
                service_price_snapshot=Decimal("0"),
                client_name_snapshot=f"c_{tg_id}",
                start_at=datetime(2026, 8, 25, 8, 0, tzinfo=UTC),  # 11:00 MSK
                end_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
                status="cancelled",
            )
            session.add(booking)
            await session.flush()
            bookings.append(booking)
        await session.commit()
        return bookings, "Europe/Moscow"


@pytest.mark.asyncio
async def test_notify_all_clients_ok(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: 2 живых клиента → notified_count=2, правильные chat_id и текст."""
    monkeypatch.setattr("bot.handlers.admin.async_session_factory", session_factory)
    bookings, tz = await _seed_stack(session_factory, client_tg_ids=[111, 222])

    bot = AsyncMock()
    count = await _notify_cancelled_clients(bookings, tz, bot, MagicMock())

    assert count == 2
    chat_ids = [c.kwargs["chat_id"] for c in bot.send_message.await_args_list]
    assert chat_ids == [111, 222]
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert all("Ваша запись отменена мастером" in t for t in texts)


@pytest.mark.asyncio
async def test_notify_flood_control_retries_once(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TelegramRetryAfter → asyncio.sleep(retry_after), retry once, notified=1.

    Паттерн из test_scheduler.py:330-352 (mock sleep против реального ожидания).
    """
    from aiogram.exceptions import TelegramRetryAfter
    from bot.handlers import admin as admin_module

    monkeypatch.setattr("bot.handlers.admin.async_session_factory", session_factory)
    bookings, tz = await _seed_stack(session_factory, client_tg_ids=[111])

    method = MagicMock()
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramRetryAfter(method=method, message="flood", retry_after=7),
        None,
    ]
    bot.send_message.await_count = 0

    async def _fake_sleep(seconds: float) -> None:
        assert seconds == 7

    monkeypatch.setattr(admin_module.asyncio, "sleep", _fake_sleep)
    count = await _notify_cancelled_clients(bookings, tz, bot, MagicMock())
    monkeypatch.undo()  # НЕ даём monkeypatch-патчу asyncio.sleep жить дальше

    assert count == 1
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_bot_none_returns_zero(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bot=None (нет Bot в контексте) → без исключений, notified=0."""
    monkeypatch.setattr("bot.handlers.admin.async_session_factory", session_factory)
    bookings, tz = await _seed_stack(session_factory, client_tg_ids=[111, 222])

    count = await _notify_cancelled_clients(bookings, tz, None, MagicMock())
    assert count == 0


@pytest.mark.asyncio
async def test_notify_mixed_one_blocked_one_alive(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 (клиент заблокировал бота) НЕ рвёт цикл: второй клиент уведомлён.

    До фикса Б1: TelegramForbiddenError не ловился → исключение вылетало из
    функции, notified_count не возвращался, оставшиеся клиенты без уведомления
    (день при этом уже закрыт). aiogram кидает Forbidden (403), а не
    BadRequest — ветка опаснее bat-request'а.
    """
    from aiogram.exceptions import TelegramForbiddenError

    monkeypatch.setattr("bot.handlers.admin.async_session_factory", session_factory)
    bookings, tz = await _seed_stack(session_factory, client_tg_ids=[111, 222])

    method = MagicMock()
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramForbiddenError(method=method, message="blocked"),  # client 111
        None,  # client 222
    ]

    # RED до фикса (исключение вылетит), GREEN после.
    count = await _notify_cancelled_clients(bookings, tz, bot, MagicMock())

    assert count == 1, "заблокировавший клиент пропускается, живой — уведомлён"
    chat_ids = [c.kwargs["chat_id"] for c in bot.send_message.await_args_list]
    assert 222 in chat_ids


@pytest.mark.asyncio
async def test_notify_bad_request_skip(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TelegramBadRequest (400) → клиент пропущен, notified=0, исключения наружу нет."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr("bot.handlers.admin.async_session_factory", session_factory)
    bookings, tz = await _seed_stack(session_factory, client_tg_ids=[111])

    bot = AsyncMock()
    bot.send_message.side_effect = TelegramBadRequest(method=MagicMock(), message="bad")

    count = await _notify_cancelled_clients(bookings, tz, bot, MagicMock())
    assert count == 0


def _booking_stub(client_id: UUID) -> Booking:
    """Detached Booking stub (для тестов, где DB не нужна — не используется пока)."""
    return Booking(
        business_id=uuid4(),
        master_id=uuid4(),
        client_id=client_id,
        service_id=uuid4(),
        service_title_snapshot="x",
        service_price_snapshot=Decimal("0"),
        client_name_snapshot="x",
        start_at=datetime(2026, 8, 25, 8, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        status="cancelled",
    )


def test_public_api_types() -> None:
    """Гигиена: сигнатура не потеряла Bot | None (иначе cmd-ргументы лягут)."""
    import inspect

    sig = inspect.signature(_notify_cancelled_clients)
    assert list(sig.parameters) == ["cancelled_bookings", "business_tz", "bot", "scheduler"]
    assert list(sig.parameters)[2] == "bot"  # позиция важна для call sites
