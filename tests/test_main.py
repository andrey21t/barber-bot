"""Tests for bot.main — entry point wiring + lifecycle hooks.

Coverage target: bot/main.py 0% → ~93% (44 stmts, 3 lines `if __name__` block excluded).

Pattern: unittest.mock.patch bot.main.{Bot,Dispatcher,CorpAiohttpSession,scheduler,
on_startup_scan,setup_logging} → call main() → assert wiring interactions.

Avoids real aiogram Bot construction (would attempt network) and real
scheduler.start/shutdown (would mutate module-level singleton state).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.handlers.admin import router as admin_router
from bot.handlers.client import router as client_router
from bot.handlers.start import router as start_router
from bot.main import _on_shutdown, _on_startup, main, setup_logging


def test_setup_logging_calls_basic_config_with_info_level_and_format() -> None:
    """setup_logging() configures root logger with INFO level + standard format."""
    with patch("bot.main.logging.basicConfig") as mock_config:
        setup_logging()

    mock_config.assert_called_once()
    kwargs = mock_config.call_args.kwargs
    assert kwargs["level"] == logging.INFO
    assert "%(asctime)s" in kwargs["format"]
    assert "%(levelname)s" in kwargs["format"]


async def test_on_startup_starts_scheduler_and_awaits_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_on_startup: scheduler.start() (sync) called, on_startup_scan awaited with args."""
    bot = AsyncMock()
    scheduler = MagicMock()
    mock_scan = AsyncMock()
    monkeypatch.setattr("bot.main.on_startup_scan", mock_scan)

    await _on_startup(bot, scheduler)

    scheduler.start.assert_called_once_with()
    mock_scan.assert_awaited_once()
    assert mock_scan.call_args.args[0] is scheduler
    assert mock_scan.call_args.args[2] is bot


async def test_on_shutdown_calls_scheduler_shutdown_no_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_on_shutdown: scheduler.shutdown(wait=False) called (sync, no await)."""
    bot = AsyncMock()
    scheduler = MagicMock()

    await _on_shutdown(bot, scheduler)

    scheduler.shutdown.assert_called_once_with(wait=False)


async def test_main_wires_routers_middleware_hooks_and_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() wires 3 routers, 2 middleware, 2 lifecycle hooks, scheduler workflow_data,
    starts polling, closes session in finally."""
    mock_bot_instance = MagicMock()
    mock_bot_instance.session = AsyncMock()

    mock_dp_instance = MagicMock()
    mock_dp_instance.start_polling = AsyncMock()

    mock_bot_class = MagicMock(return_value=mock_bot_instance)
    mock_dp_class = MagicMock(return_value=mock_dp_instance)
    mock_session_class = MagicMock()
    mock_scheduler = MagicMock()
    mock_setup_logging = MagicMock()

    monkeypatch.setattr("bot.main.Bot", mock_bot_class)
    monkeypatch.setattr("bot.main.Dispatcher", mock_dp_class)
    monkeypatch.setattr("bot.main.CorpAiohttpSession", mock_session_class)
    monkeypatch.setattr("bot.main.scheduler", mock_scheduler)
    monkeypatch.setattr("bot.main.setup_logging", mock_setup_logging)

    await main()

    mock_setup_logging.assert_called_once_with()
    mock_bot_class.assert_called_once()
    bot_kwargs = mock_bot_class.call_args.kwargs
    assert "token" in bot_kwargs
    assert bot_kwargs["session"] is mock_session_class.return_value

    mock_dp_class.assert_called_once_with()
    mock_dp_instance.__setitem__.assert_called_once_with("scheduler", mock_scheduler)

    assert mock_dp_instance.include_router.call_count == 3
    router_calls = mock_dp_instance.include_router.call_args_list

    assert router_calls[0].args[0] is start_router
    assert router_calls[1].args[0] is admin_router
    assert router_calls[2].args[0] is client_router

    assert mock_dp_instance.message.middleware.call_count == 1
    assert mock_dp_instance.callback_query.middleware.call_count == 1

    assert mock_dp_instance.startup.register.call_count == 1
    assert mock_dp_instance.shutdown.register.call_count == 1

    mock_dp_instance.start_polling.assert_awaited_once_with(mock_bot_instance)
    mock_bot_instance.session.close.assert_awaited_once()


async def test_main_finally_closes_session_on_polling_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() — dp.start_polling raises → finally still awaits bot.session.close()."""
    mock_bot_instance = MagicMock()
    mock_bot_instance.session = AsyncMock()

    mock_dp_instance = MagicMock()
    mock_dp_instance.start_polling = AsyncMock(side_effect=RuntimeError("polling crashed"))

    monkeypatch.setattr("bot.main.Bot", MagicMock(return_value=mock_bot_instance))
    monkeypatch.setattr("bot.main.Dispatcher", MagicMock(return_value=mock_dp_instance))
    monkeypatch.setattr("bot.main.CorpAiohttpSession", MagicMock())
    monkeypatch.setattr("bot.main.scheduler", MagicMock())
    monkeypatch.setattr("bot.main.setup_logging", MagicMock())

    with pytest.raises(RuntimeError, match="polling crashed"):
        await main()

    mock_bot_instance.session.close.assert_awaited_once()


async def test_main_registers_on_startup_and_on_shutdown_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() registers _on_startup and _on_shutdown as lifecycle callbacks (not lambdas)."""
    mock_dp_instance = MagicMock()
    mock_dp_instance.start_polling = AsyncMock()
    mock_bot_instance = MagicMock()
    mock_bot_instance.session = AsyncMock()

    monkeypatch.setattr("bot.main.Bot", MagicMock(return_value=mock_bot_instance))
    monkeypatch.setattr("bot.main.Dispatcher", MagicMock(return_value=mock_dp_instance))
    monkeypatch.setattr("bot.main.CorpAiohttpSession", MagicMock())
    monkeypatch.setattr("bot.main.scheduler", MagicMock())
    monkeypatch.setattr("bot.main.setup_logging", MagicMock())

    await main()

    startup_callback = mock_dp_instance.startup.register.call_args.args[0]
    shutdown_callback = mock_dp_instance.shutdown.register.call_args.args[0]
    assert startup_callback is _on_startup
    assert shutdown_callback is _on_shutdown
