"""Tests for bot.session — corp CA bundle auto-detection + CorpAiohttpSession.

Coverage target: bot/session.py 0% → 100% (24 stmts).

build_ssl_context() branch matrix:
- no env / no ~/okko-ca.pem → default ctx, no load_verify_locations call
- env set, file exists → load_verify_locations called with that path
- env set, SSLError on load → continues to ~/okko-ca.pem (also exists → loads)
- CorpAiohttpSession() — _connector_init['ssl'] replaced with build_ssl_context() result
"""

from __future__ import annotations

import ssl
from pathlib import Path
from unittest.mock import patch

import pytest
from bot.config import get_settings
from bot.session import CorpAiohttpSession, build_ssl_context


def test_build_ssl_context_no_env_no_pem_returns_default_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No BARBER_SSL_CA_BUNDLE env, no ~/okko-ca.pem → default ctx, no extra CA loaded."""
    monkeypatch.delenv("BARBER_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with patch.object(ssl.SSLContext, "load_verify_locations") as mock_load:
        ctx = build_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    mock_load.assert_not_called()


def test_build_ssl_context_env_file_exists_loads_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Env points to existing file → load_verify_locations called once with that path."""
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("")
    monkeypatch.setenv("BARBER_SSL_CA_BUNDLE", str(ca_file))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with patch.object(ssl.SSLContext, "load_verify_locations") as mock_load:
        ctx = build_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    mock_load.assert_called_once_with(str(ca_file))


def test_build_ssl_context_env_sserror_continues_to_home_pem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Env file raises SSLError → continue to ~/okko-ca.pem (exists → loads → break)."""
    ca_env = tmp_path / "ca-env.pem"
    ca_env.write_text("")
    ca_home = tmp_path / "okko-ca.pem"
    ca_home.write_text("")
    monkeypatch.setenv("BARBER_SSL_CA_BUNDLE", str(ca_env))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with patch.object(
        ssl.SSLContext,
        "load_verify_locations",
        side_effect=[ssl.SSLError("simulated corp CA error"), None],
    ) as mock_load:
        ctx = build_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    assert mock_load.call_count == 2


def test_corp_aiohttp_session_init_sets_ssl_in_connector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CorpAiohttpSession() — _connector_init['ssl'] is replaced with build_ssl_context() result.

    Isolates ~/okko-ca.pem + BARBER_SSL_CA_BUNDLE env (S1 from code-review):
    on dev machine ~/okko-ca.pem exists (corp CA), on CI it doesn't — test must pass
    in both environments without relying on real file presence.
    """
    monkeypatch.delenv("BARBER_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    session = CorpAiohttpSession()

    assert "ssl" in session._connector_init
    assert isinstance(session._connector_init["ssl"], ssl.SSLContext)


def test_corp_aiohttp_session_init_passes_kwargs_to_super(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CorpAiohttpSession(limit=42) — kwargs forwarded to AiohttpSession.__init__.

    Isolation: see test_corp_aiohttp_session_init_sets_ssl_in_connector (S1).
    """
    monkeypatch.delenv("BARBER_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    session = CorpAiohttpSession(limit=42)

    assert session._connector_init["limit"] == 42
    assert isinstance(session._connector_init["ssl"], ssl.SSLContext)


def test_build_ssl_context_env_path_does_not_exist_falls_through_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Env path doesn't exist → is_file() False → tries ~/okko-ca.pem (exists → loads)."""
    ca_env_missing = tmp_path / "missing-ca.pem"  # not created
    ca_home = tmp_path / "okko-ca.pem"
    ca_home.write_text("")
    monkeypatch.setenv("BARBER_SSL_CA_BUNDLE", str(ca_env_missing))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with patch.object(ssl.SSLContext, "load_verify_locations") as mock_load:
        ctx = build_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    mock_load.assert_called_once_with(str(ca_home))


# ============================================================
# build_session — optional Telegram API proxy (Session 5.53)
# ============================================================


def test_build_session_flag_off_returns_default_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """5.53: TELEGRAM_API_BASE_URL empty/absent → direct api.telegram.org
    (feature flag OFF, default — zero behavior change vs pre-5.53).
    """
    monkeypatch.delenv("BARBER_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    settings = get_settings()
    monkeypatch.setattr(settings, "TELEGRAM_API_BASE_URL", "", raising=False)
    monkeypatch.setattr("bot.session.get_settings", lambda: settings)

    from bot.session import build_session

    session = build_session()

    # Default aiogram APIServer: api root is api.telegram.org, no secret prefix.
    assert session.api.api_url(token="T", method="m") == "https://api.telegram.org/botT/m"


def test_build_session_flag_on_routes_via_proxy_with_secret_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """5.53: TELEGRAM_API_BASE_URL set → all Bot API calls go through the
    Worker proxy. from_base keeps the secret path prefix — the Worker
    (deploy/telegram-proxy-worker.js) strips it and proxies upstream.
    """
    monkeypatch.delenv("BARBER_SSL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    proxy = "https://telegram-proxy.example.workers.dev/SECRET123"
    settings = get_settings()
    monkeypatch.setattr(settings, "TELEGRAM_API_BASE_URL", proxy, raising=False)
    monkeypatch.setattr("bot.session.get_settings", lambda: settings)

    from bot.session import build_session

    session = build_session()

    assert (
        session.api.api_url(token="T", method="sendMessage")
        == f"{proxy}/botT/sendMessage"
    )
    assert (
        session.api.file_url(token="T", path="f/p")
        == f"{proxy}/file/botT/f/p"
    )
    # corp CA context still applied (proxy does not disable TLS inspection fix).
    assert isinstance(session._connector_init["ssl"], ssl.SSLContext)
