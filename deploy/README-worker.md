# Cloudflare Worker proxy for api.telegram.org

> **СТАТУС: ЗАДЕПЛОЕН 2026-09-11 (сессия 5.54), ВКЛЮЧЁН НА ПРОДЕ.**
> URL: `https://telegram-proxy.mybarber-hair-bot.workers.dev`, secret в
> `~/.config/opencode/references/barber-bot-deploy-credentials.md` (WORKER_SECRET).
> VPS `.env`: `TELEGRAM_API_BASE_URL=<WORKER_URL>/<WORKER_SECRET>`.
> Нюанс деплоя: субдомен workers.dev аккаунта был не зарегистрирован — команда
> `wrangler subdomain` не существует, регистрация через CF API:
> `PUT /accounts/<account_id>/workers/subdomain` body `{"subdomain":"mybarber-hair-bot"}`
> (Bearer = oauth_token из `~/Library/Preferences/.wrangler/config/default.toml`).
> После регистрации edge-сертификат выпускается ~1-2 мин — до него TLS handshake failure.

## Зачем

VPS (RU-хостер) ловит регулярные `Connection reset by peer` к api.telegram.org
(13+ раз за 2 дня, вспышками — handoff 5.51). Причина: сетевой путь RU→Telegram.
Cloudflare до api.telegram.org ходит надёжно, VPS до Cloudflare тоже.
Прокси через Worker убирает RU→Telegram leg.

## Деплой (~10 минут, один раз)

```bash
cd deploy
npx wrangler login          # откроет браузер, login в твой CF-аккаунт
# Сгенерируй секрет: openssl rand -hex 16
npx wrangler secret put WORKER_SECRET   # вставь секрет
npx wrangler deploy
```

Вывод deploy даст URL вида `https://telegram-proxy.<account>.workers.dev`.
Проверь руками:

```bash
curl -s "https://telegram-proxy.<account>.workers.dev/<SECRET>/bot<BOT_TOKEN>/getMe"
# {"ok":true,"result":{...id":"8935808150"...}}  — прокси жив
curl -s "https://telegram-proxy.<account>.workers.dev/WRONG/bot<BOT_TOKEN>/getMe"
# {"error":"Not Found"} — секрет работает как guard
```

## Включение на VPS

В `/opt/barber-bot/.env` добавить (BOT_TOKEN не меняется):

```
TELEGRAM_API_BASE_URL=https://telegram-proxy.<account>.workers.dev/<SECRET>
```

Затем `cd /opt/barber-bot && docker compose up -d --build` (или рестарт
контейнера — env читается на старте). В логах строка
`Telegram API via proxy: https://...` подтвердит, что прокси активен.

Выключить прокси: убрать переменную (или оставить пустой) и рестарт.

## Лимиты free tier

100k запросов/день. Long polling: ~2 req/min ≈ 3k/день. Запас 30x.
Если бот когда-нибудь уйдёт на webhooks — ещё меньше.

## Откат

Убрать `TELEGRAM_API_BASE_URL` из .env → бот вернётся на api.telegram.org
прямо, без кода-изменений (feature flag, по умолчанию выключен).
