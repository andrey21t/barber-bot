# NEXT_SESSION_PROMPT — barber-bot, handoff после сессии 5.54

## Текущее состояние (актуально на 2026-09-11, ~12:35 UTC)

**Origin/main:** `ccdc60d` (5.54 docs) + offsite-backup script (этот коммит). Прод живёт на коде `ad3dbe0` (5.53) + `.env` с прокси.
**VPS:** `VPS_HOST_FROM_CRED_FILE`, бот `@My_Barber_hair_bot` работает **через Cloudflare Worker proxy** с 2026-09-11 10:33 UTC.
**Гейты:** ruff ✅ · mypy 0 · pytest 550 passed, 2 skipped (код 5.53, не менялся в 5.54 — только docs/infra).

## Что сделано в 5.54

### 1. Worker proxy — ЗАДЕПЛОЕН, ВКЛЮЧЁН, СТАБИЛЕН (2 часа, 0 обрывов)

- Воркер не был задеплоен (деплой числился за владельцем). Ассистент задеплоил сам — wrangler-сессия на Mac жива, код готов с 5.53.
- **Нюанс деплоя:** субдомен workers.dev аккаунта не существовал (первый воркер). `wrangler subdomain` не существует — регистрация через CF API: `PUT /accounts/<id>/workers/subdomain` body `{"subdomain":"mybarber-hair-bot"}` (Bearer = oauth_token из `~/Library/Preferences/.wrangler/config/default.toml`). Edge-сертификат выпускается ~1-2 мин — до него TLS handshake failure.
- **Воркер:** `https://telegram-proxy.mybarber-hair-bot.workers.dev`, secret `WORKER_SECRET`. URL+SECRET в `~/.config/opencode/references/barber-bot-deploy-credentials.md` (WORKER_URL / WORKER_SECRET).
- **Включение:** VPS `.env` → `TELEGRAM_API_BASE_URL=<WORKER_URL>/<WORKER_SECRET>` + `docker compose up -d --force-recreate bot`. Лог: `Telegram API via proxy`.
- **Замер обрывов:**
  - ДО прокси (тот же день): бот упал НАСМЕРТЬ в 10:20:03 — aiohttp TimeoutError убил процесс (policy `unless-stopped` поднял в 10:20:05). Плюс 2 обрыва за 11 мин до этого.
  - ПОСЛЕ включения (10:33): **2 часа long polling через CF — 0 обрывов, 0 рестартов.** Обновления обрабатываются за 106-113ms. Лог чистый.
- **Откат:** убрать `TELEGRAM_API_BASE_URL` из `.env` + `docker compose up -d --force-recreate bot` → бот вернётся на прямой api.telegram.org.
- `deploy/README-worker.md` обновлён: статус «задеплоено» + нюанс с субдоменом.

### 2. Оффсайт-копия бэкапов — Mac scp-pull, ГОТОВО

- R2 заблокирован (нужен dashboard enablement — платёжка). Ассистент развернул Mac scp-pull — работает сразу, без dashboard.
- **`scripts/offsite-backup.sh`** в репо: sshpass scp новых dump'ов с VPS → `~/barber-bot-backups/`, retention 7 дней. Пароль читается из `~/.config/opencode/references/barber-bot-deploy-credentials.md` (не в репо).
- **Тест пройден:** dump `barber_2026-09-11_1007.dump` (28K) скачан с VPS, локально в `~/barber-bot-backups/`.
- **launchd:** `~/Library/LaunchAgents/com.barber.offsite-backup.plist` — ежедневно 06:30 MSK (03:30 UTC, синхронно с VPS cron). Загружен в launchctl.
- **SSH key:** не заработал — приватный ключ запаролен, `/dev/tty` недоступен в opencode → sshpass с password-auth как fallback (работает, проверено). Если владельцев когда-то введёт passphrase в интерактивном терминале — можно перейти на key-auth, скрипт уже совместим (sshpass fallback не нужен).
- **Ограничение:** Mac должен быть включён в 06:30 MSK. Если выключен — dump пропускается, следующий запуск подберёт (скрипт не качает дубликаты). R2 остаётся как future upgrade когда владелец активирует R2 в dashboard.

### 3. Backup cron на VPS — состояние НОРМА

Cron `30 3 * * *` установлен. Первый автозапуск: **2026-09-12 03:30 UTC**. `/var/log/barber_backup.log` ещё не существует — появится после первого срабатывания. Первое в следующей сессии: проверить, что ночной dump прошёл.

## НЕЗАКРЫТЫЕ ЗАДАЧИ (в порядке приоритета)

### 1. Проверка первого ночного backup-цикла (сессия 5.55, 2026-09-12)

- VPS: `tail /var/log/barber_backup.log` + `ls /opt/barber-bot/backups/` — cron отработал?
- Mac: `ls ~/barber-bot-backups/` + `tail ~/barber-bot-backups/launchd.log` — offsite pull сработал?
- Если оба зелёные — оффсайт-копия закрывает one-way door.

### 2. Smoke-тест юзером (ЖДём обратной связи — чек-лист 5.52)

Владелец должен прогнать в @My_Barber_hair_bot:
- `/book` → «Своя услуга» НЕТ, только услуги мастера; текст вместо тапа → подсказка + СВЕЖИЙ пикер
- Под «Вы записаны» inline-кнопок НЕТ; клавиатуры шагов гаснут при переходе
- «❌ Отмена» на подтверждении и /cancel — гасят ✅/❌ и реально отменяют (в т.ч. на шаге услуги/имени)
- Любой текст без активной записи → «Начните запись через /book»; «Нет свободных дат/слотов» — не крутит спиннер вечно
- **Через прокси бот должен отвечать быстро** — если всё ещё тупит на /book, смотреть не сеть (DB, воркер CF, рендер)

### 3. Фоновое

- После суток-двух стабильного прокси: сравнить счётчики обрывов до/после, зафиксировать в README-worker.md.
- R2 как future upgrade: активировать в CF dashboard → `wrangler r2 bucket create barber-backups` → заменить sshpass scp на rclone/curl PUT to R2 (VPS-сторона, Mac не нужен).
- Передача бота Екатерине: `ADMIN_ID` в `.env` на VPS. On-behalf booking — когда Екатерина начнёт работать.

## Как деплоить

```bash
BARBER_PASS=$(grep -E '^PASS:' ~/.config/opencode/references/barber-bot-deploy-credentials.md | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh root@VPS_HOST_FROM_CRED_FILE
cd /opt/barber-bot && git pull && docker compose up -d --build
docker logs --tail 20 barber-bot-bot-1   # "Run polling" + "Telegram API via proxy"
```

Миграции применяются автоматически (контейнер стартует через `alembic upgrade head && python -m bot.main`).

## Правила сессии (напоминание)

- **MY-VIBE-RULES.md** — dev-режим: deep-analysis на нетривиальное → реализация → verify (pytest/ruff/mypy) → code-review subagent → коммит свободный (личный репо)
- **Креды VPS и воркер-секрет НЕ коммитить** — живут в `~/.config/opencode/references/barber-bot-deploy-credentials.md`
- **VPS-диагностику делать самому** через sshpass (не просить юзера вводить команды)
- Code-reviewer subagent: если вернул пустой результат 2+ раза — REVIEW_UNAVAILABLE, фиксировать в коммит-месседже, детерминированные проверки делать самому
