# NEXT_SESSION_PROMPT — barber-bot, handoff после сессии 5.54

## Текущее состояние (актуально на 2026-09-11, ~10:50 UTC)

**Origin/main:** docs-коммит 5.54 (README-worker + этот handoff). Код продакшена не менялся: **прод живёт на `ad3dbe0`** (код 5.53) + `.env` с включённым прокси (env не в git).
**VPS:** `VPS_HOST_FROM_CRED_FILE`, бот `@My_Barber_hair_bot` работает **через Cloudflare Worker proxy** с 2026-09-11 10:33 UTC.
**Гейты:** ruff ✅ · mypy 0 · pytest 550 passed, 2 skipped (5.53, код не менялся в 5.54 — только docs/infra).

## ГЛАВНОЕ СОБЫТИЕ 5.54: Worker proxy ЗАДЕПЛОЕН И ВКЛЮЧЁН НА ПРОДЕ

### Что было и что сделано

- Воркер **не был задеплоен** (деплой числился за владельцем) — но wrangler-сессия на Mac жива, код и README готовы с 5.53. Ассистент задеплоил сам по README-плану.
- **Субдомен workers.dev аккаунта не существовал** (первый воркер аккаунта). `wrangler subdomain` не существует — регистрация через CF API: `PUT /accounts/<id>/workers/subdomain` body `{"subdomain":"mybarber-hair-bot"}` (Bearer = oauth_token из `~/Library/Preferences/.wrangler/config/default.toml`). Edge-сертификат выпускается ~1-2 мин — до него TLS handshake failure (не пугаться, просто подождать).
- **Воркер:** `https://telegram-proxy.mybarber-hair-bot.workers.dev`, имя `telegram-proxy`, secret `WORKER_SECRET`. **URL+SECRET сохранены** в `~/.config/opencode/references/barber-bot-deploy-credentials.md` (WORKER_URL / WORKER_SECRET) — НЕ коммитить.
- Проверено с VPS: getMe через воркер `{"ok":true}`, wrong-secret → 404 (гвард работает), ~0.19s.
- **Включение на VPS:** `/opt/barber-bot/.env` → `TELEGRAM_API_BASE_URL=<WORKER_URL>/<WORKER_SECRET>` + `docker compose up -d --force-recreate bot` (без build — образ не менялся). Лог подтвердил: `Telegram API via proxy`.

### Замер обрывов (главная метрика жалобы №1 «медленный старт»)

- **До прокси (тот же день):** бот упал НАСМЕРТЬ в 10:20:03 — aiohttp TimeoutError к api.telegram.org убил процесс (policy `unless-stopped` поднял в 10:20:05, Restarts=1). Плюс 2 обрыва за 11 мин до этого. Это живое подтверждение диагноза 5.51 в реальном времени.
- **После включения (10:33):** 10 минут long polling через CF (~20+ запросов) — **0 обрывов, 0 рестартов**. Лог чистый.
- **Мониторинг продолжать:** сутки-двое `docker logs --since <t> barber-bot-bot-1 | grep -ciE 'reset by peer|timeout error'` + `docker inspect barber-bot-bot-1 --format '{{.RestartCount}}'` (должен замереть). CF free tier: 100k req/day, у нас ~3k — запас 30x.

### Откат (если что-то пойдёт не так)

Убрать `TELEGRAM_API_BASE_URL` из `.env` + `docker compose up -d --force-recreate bot` → бот вернётся на прямой api.telegram.org. Код не менять (feature flag).

## Backup cron — состояние НОРМА, первый автозапуск ещё не наступил

Cron `30 3 * * *` установлен сегодня в 10:18 UTC. Скрипт `scripts/backup.sh` на месте, ручной dump от restore-теста лежит в `backups/` (26KB). `/var/log/barber_backup.log` ЕЩЁ НЕ СУЩЕСТВУЕТ — это не баг: лог появится после первого срабатывания cron **2026-09-12 03:30 UTC**. Первое, что сделать в следующей сессии: проверить, что ночной dump прошёл (файл в `/opt/barber-bot/backups/`, строки в логе).

## НЕЗАКРЫТЫЕ ЗАДАЧИ (в порядке приоритета)

### 1. Smoke-тест юзером (ЖДЁМ ОБРАТНОЙ СВЯЗИ — чек-лист 5.52)

Владелец должен прогнать в @My_Barber_hair_bot чек-лист (полный список — в git-истории handoff 5.53 `a19b3df`, кратко):
- `/book` → «Своя услуга» НЕТ, только услуги мастера; текст вместо тапа → подсказка + СВЕЖИЙ пикер
- Под «Вы записаны» inline-кнопок НЕТ; клавиатуры шагов гаснут при переходе
- «❌ Отмена» на подтверждении и /cancel — гасят ✅/❌ и реально отменяют (в т.ч. на шаге услуги/имени)
- Любой текст без активной записи → «Начните запись через /book»; «Нет свободных дат/слотов» — не крутит спиннер вечно

### 2. Оффсайт-копия бэкапов (РЕШЕНИЕ ВЛАДЕЛЬЦА, ассистент реализует любой выбор)

Dump'ы только на VPS — потеря VPS = потеря бэкапов. Варианты:
- **Cloudflare R2 (рекомендую — CF-аккаунт уже активно используется воркером, free 10GB):** `wrangler r2 bucket create barber-backups` + upload. Реализация: rclone на VPS с R2 token (S3-compatible) ИЛИ cron с Mac (scp с VPS → `wrangler r2 object put`).
- **Mac pull:** scp/rsync dump'ов на Mac по cron — 2 строки, но Mac должен быть включён.
- Любой S3.

### 3. Фоновое

- После суток-двух стабильного прокси: сравнить счётчики обрывов до/после, зафиксировать в README-worker.md.
- Передача бота Екатерине: `ADMIN_ID` в `.env` на VPS (сейчас ID владельца — он сам тестирует). On-behalf booking — когда Екатерина начнёт работать.
- Тот же smoke-чеклист теперь проверяет и «медленный старт» — если через прокси бот всё равно тупит на /book, смотреть в другую сторону (не сеть: DB, воркер CF, рендер).

## Как деплоить

```bash
BARBER_PASS=$(grep -E '^PASS:' ~/.config/opencode/references/barber-bot-deploy-credentials.md | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh root@VPS_HOST_FROM_CRED_FILE
cd /opt/barber-bot && git pull && docker compose up -d --build
docker logs --tail 20 barber-bot-bot-1   # smoke-check: "Run polling" + "Telegram API via proxy"
```

Миграции применяются автоматически (контейнер стартует через `alembic upgrade head && python -m bot.main`).

## Правила сессии (напоминание)

- **MY-VIBE-RULES.md** — dev-режим: deep-analysis на нетривиальное → реализация → verify (pytest/ruff/mypy) → code-review subagent → коммит свободный (личный репо)
- **Креды VPS и воркер-секрет НЕ коммитить** — живут в `~/.config/opencode/references/barber-bot-deploy-credentials.md`
- **VPS-диагностику делать самому** через sshpass (не просить юзера вводить команды)
- Code-reviewer subagent: если вернул пустой результат 2+ раза — REVIEW_UNAVAILABLE, фиксировать в коммит-месседже, детерминированные проверки делать самому
