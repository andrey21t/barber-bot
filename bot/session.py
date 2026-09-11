import os
import ssl
from pathlib import Path
from typing import Any

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from bot.config import get_settings

# Corp-сеть (Okko) перехватывает HTTPS через TLS inspection, подменяет сертификаты.
# aiogram по умолчанию использует certifi (не видит corp CA) →
# CERTIFICATE_VERIFY_FAILED. Решение — подгрузить corp CA bundle в ssl context.
#
# Auto-detect порядок (если файл существует — добавляем в verify locations):
#   1. env BARBER_SSL_CA_BUNDLE (явный override)
#   2. ~/okko-ca.pem (стандартный Okko corp CA, см. ~/.zshrc NODE_EXTRA_CA_CERTS)
#   3. системный CA (default via ssl.create_default_context)


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    candidate_paths: list[Path] = []
    env_path = os.environ.get("BARBER_SSL_CA_BUNDLE")
    if env_path:
        candidate_paths.append(Path(env_path))
    candidate_paths.append(Path.home() / "okko-ca.pem")
    for p in candidate_paths:
        try:
            if p.is_file():
                ctx.load_verify_locations(str(p))
                break
        except (OSError, ssl.SSLError):
            continue
    return ctx


class CorpAiohttpSession(AiohttpSession):
    """AiohttpSession с auto-detected corp CA bundle для TLS inspection."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connector_init["ssl"] = build_ssl_context()


def build_session() -> CorpAiohttpSession:
    """Session factory (Session 5.53): optional Telegram API proxy.

    TELEGRAM_API_BASE_URL empty/absent (default) → direct api.telegram.org,
    zero behavior change vs pre-5.53.

    TELEGRAM_API_BASE_URL set (e.g. https://x.workers.dev/SECRET) → all Bot
    API calls go through the Cloudflare Worker proxy (VPS network fix,
    handoff 5.51: recurring connection resets to api.telegram.org from the
    RU hoster). TelegramAPIServer.from_base keeps the secret path prefix:
    api_url = <base>/bot<token>/<method> — the Worker strips <base>'s secret
    part and proxies to https://api.telegram.org/bot<token>/<method>.
    """
    proxy_base = get_settings().TELEGRAM_API_BASE_URL.strip()
    if proxy_base:
        return CorpAiohttpSession(api=TelegramAPIServer.from_base(proxy_base))
    return CorpAiohttpSession()
