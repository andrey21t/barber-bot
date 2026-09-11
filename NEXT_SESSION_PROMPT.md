# NEXT_SESSION_PROMPT — barber-bot, handoff после сессии 5.53

## Текущее состояние (актуально на 2026-09-11)

**Origin/main:** `ad3dbe0` (Worker proxy flag OFF + backup infra, pushed + DEPLOYED на VPS)
**VPS:** `VPS_HOST_FROM_CRED_FILE`, бот `@My_Barber_hair_bot` запущен, контейнер на свежем коммите
**Гейты зелёные:** ruff ✅ · mypy 0 · pytest 550 passed, 2 skipped
**Прод-проверка 5.53:** лог `Telegram API: direct api.telegram.org` (flag OFF — новый код живой), reminder-джоба отработала в 10:19 UTC, 0 сетевых ошибок с 09:18

## Исходная задача сессии 5.51 — 3 жалобы пользователя (2026-09-11)

1. **Медленный старт записи** — «заходишь, бот долго не отвечает»
2. **«Своя услуга»** — клиент мог ввести текстом свою услугу → календарь/`/today` врали о длительности (free-text не имеет duration_minutes → молча подставлялся SERVICE_DEFAULT_DURATION_MIN=60)
3. **Мёртвые кнопки после записи** — inline [Мои записи][Ещё запись] под «Вы записаны» дублировали постоянные кнопки внизу + старые клавиатуры шагов флоу оставались живыми

## Что сделано в 5.51

### Коммит `6ef4bd7` (deployed) — жалобы 2 + 3

- **«Своя услуга» удалена полностью:** кнопка (keyboards/client.py), handler `service_custom_cb`, free-text ветки. Услуги только тапом из списка мастера. Typed text → подсказка «Пожалуйста, выберите услугу кнопкой 👇» (`service_msg` — hint no-op). Архивная/удалённая услуга в `service_picker_cb` → свежий пикер из DB. Нет услуг → `state.clear` + «Мастер пока не настроил услуги. Загляните позже 🙏» (все 3 точки: `_process_selected_date`, `book_back_to_service_cb`, fresh-picker fallback)
- **Мёртвые клавиатуры:** хелпер `_clear_source_keyboard` (edit_reply_markup None, InaccessibleMessage-safe, suppress TelegramBadRequest) на каждом шаге флоу. `post_booking_keyboard` удалена — success-сообщение голое. Бонус: «❌ Отмена» (BookCancelCallbackData) была без handler'а с 5.27 — добавлен `cancel_flow_cb` (StateFilter `*`)
- **mypy pre-existing фикс:** `name_pre_fill_yes_cb` isinstance-narrowing
- Тесты переписаны под новые контракты (13 падавших → новые)

### Коммит `ef30201` (deployed) — code-review warnings W1-W3

- **W1:** `no_state_callback_fallback` (State(None) fallback для stale кнопок после session timeout) теперь стрипает клавиатуру — раньше каждый тап повторял alert «Сессия истекла» бесконечно
- **W2:** `cancel_flow_cb` + `cancel_msg` ветвятся по `transfer_booking_id` (читается ДО state.clear). Отмена переноса теперь ведёт в `/mybookings`, а не `/slots` (transfer заливает is_slots_path=True по B.1 — без ветки хинт врал)
- **W3:** `_render_summary_and_set_confirming` сохраняет `summary_msg_id` в FSM; `cancel_msg` (/cancel из confirming) гасит ✅/❌ клавиатуру на summary через `bot.edit_message_reply_markup` (suppress TelegramBadRequest)
- +5 тестов на W1-W3

### Диагностика жалобы 1 (медленный старт) — ПРОВЕДЕНА, вердикт

Зашёл на VPS сам (креды: `~/.config/opencode/references/barber-bot-deploy-credentials.md`, sshpass). Результаты:

- **409 Conflict / двойной бот — НЕТ.** Один контейнер, лишних процессов нет
- **Сеть VPS → api.telegram.org — РЕАЛЬНАЯ причина.** В логах регулярные `Connection reset by peer` (Errno 104) + `Request timeout error` — 13 раз за 2 дня (2026-09-09: 2, 09-10: 9, 09-11: 1), вспышками (вечер 10-го — 3 подряд за 3 мин). Один таймаут случился прямо во время обработки апдейта юзера — ответ терялся
- Механика: long polling не видит `/book` пока соединение не восстановится
- Пинг curl сейчас быстрый (0.11-0.13s), но обрывы пачками — типично для RU-хостера к api.telegram.org

## Что сделано в 5.53 (коммит `ad3dbe0`, deployed, flag OFF)

### Backup БД — ГОТОВО ПОЛНОСТЬЮ

- `/opt/barber-bot/scripts/backup.sh` на VPS: pg_dump -Fc (сжатый) → `/opt/barber-bot/backups/`, верификация TOC через `pg_restore --list` внутри скрипта (битый dump = FATAL exit 1)
- **Cron:** `30 3 * * *` (nightly 03:30 UTC = 06:30 MSK), лог → `/var/log/barber_backup.log`
- **Retention:** 7 daily копий (диск VPS 79% занят; dump ~26KB при БД 8.8MB — запас огромный)
- **Proof-of-restore выполнен:** первый dump восстановлен в одноразовый `postgres:16-alpine` контейнер — bookings:8, clients:3, services:5 на месте. Restore-контейнер удалён
- ⚠️ **ОФФСАЙТ-КОПИИ НЕТ** — dump'ы лежат только на VPS. Потеря VPS = потеря бэкапов тоже. Варианты (решение владельца): R2 bucket (free 10GB) / S3 / cron pull на Mac через rsync/scp

### Cloudflare Worker proxy для Telegram API — КОД ГОТОВ, ДЕПЛОЙ ЗА ВЛАДЕЛЬЦЕМ

Root cause жалобы «бот долго не отвечает» (5.51): обрывы VPS→api.telegram.org пачками. Решение готово к включению:

- `deploy/telegram-proxy-worker.js` — воркер: `/<WORKER_SECRET>/bot<token>/<method>` → api.telegram.org; без секрета 404 (не open relay)
- `deploy/README-worker.md` — пошаговый деплой + проверка + откат
- Код бота: `TELEGRAM_API_BASE_URL` в .env (пусто = дефолт, сейчас OFF); `build_session()` в `bot/session.py`; лог-строка routing-режима на старте
- **Что осталось (владелец):** `npx wrangler login` → `wrangler secret put WORKER_SECRET` → `wrangler deploy` → положить URL+секрет в `/opt/barber-bot/.env` как `TELEGRAM_API_BASE_URL` → `docker compose up -d --build`. Ассистент может сам дописать .env и рестартнуть, когда URL готов

### Suggestions S1-S5 (из ревью 5.51) — ВСЕ закрыты

- **S1:** `slot_cb`/`slot_30_cb` стрипают клавиатуру ДО defensive-проверок — терминальные ветки больше не уходят с живыми мёртвыми кнопками
- **S2:** устаревшие комментарии про service_msg исправлены по всему client.py
- **S3:** `service_msg` self-healing: стрипает старый пикер по `service_picker_msg_id` из FSM + рендерит СВЕЖИЙ пикер (удалённый пикер больше не dead-end). Трекер-id пишется во всех 5 точках рендера пикера. Хелпер `_fetch_active_services` (дедуп 3 копий)
- **S4:** мёртвый `client_inline_menu()` удалён (reply keyboard — live entry с B.13)
- **S5:** `noop_cb` — handler для плейсхолдеров «Нет свободных дат/слотов» (вечный спиннер ушёл). Зарегистрирован ПОСЛЕ `no_state_callback_fallback` (порядок пинов тестом)

### W1 follow-up

- `name_msg`/`service_msg` фильтры + `~F.text.startswith("/")` — /cancel доходит до `cancel_msg` (раньше catch-all съедал: /cancel мог стать client_name). `cancel_msg` стрипает ОБА трекер-id: `summary_msg_id` + `service_picker_msg_id`

### Багфикс (найден падением integration-теста, существовал с 5.9!)

- `admin_no_state_catchall_text` (admin.py): для non-admin `return` → `raise SkipHandler`. Прежний `return` МОЛЧА съедал update (aiogram: первый сматченный handler = обработано, проваливания НЕТ) → любой НЕ-админ в State(None), набравший текст, не получал ответа вообще. SkipHandler пробрасывает к `client_router.no_state_fallback`. Пинован юнит-тестами + integration через `dp.feed_update`

## НЕЗАКРЫТЫЕ ЗАДАЧИ (в порядке приоритета)

### 1. Включение Worker proxy (после wrangler deploy владельцем)

Порядок: владелец деплоит воркер (README-worker.md, ~10 мин) → даёт ассистенту URL вида `https://telegram-proxy.<account>.workers.dev` + SECRET → ассистент сам: `.env` на VPS (`TELEGRAM_API_BASE_URL=...`) → `docker compose up -d --build` → проверка лога (`Telegram API via proxy`) → smoke getMe через воркер → мониторинг обрывов (`docker logs | grep -ci 'reset\|timeout'`) до/после.

Если что-то не работает — что смотреть: (1) `curl "https://<worker-url>/<SECRET>/bot<TOKEN>/getMe"` — воркер жив? (2) wrong-secret → 404 (guard ок); (3) в логах бота `Telegram API via proxy` появилась? (4) wrangler не установлен → `npx wrangler` (Node 17+); (5) free tier CF: 100k req/day, у нас ~3k — если упрутся, смотреть dashboard Usage.

### 2. Оффсайт-копия бэкапов (решение владельца)

Сейчас dump'ы ТОЛЬКО на VPS — one-way door остаётся открытым наполовину. Варианты: Cloudflare R2 (free 10GB, логичен если уже есть CF-аккаунт для воркера) / любой S3 / cron с Mac'а (`scp` в 2 строчки). Ассистент реализует любой выбранный вариант.

### 3. Smoke-тест юзером (ЖДЁМ обратной связи)

Пользователь должен прогнать в @My_Barber_hair_bot чек-лист (проверяет 5.52):
- `/book` → дата → кнопки «Своя услуга» НЕТ, только услуги мастера
- Набрать текст вместо тапа → подсказка «выберите услугу кнопкой» + СВЕЖИЙ пикер рядом (5.52 S3)
- Удалить сообщение пикера, набрать текст снова → снова свежий пикер (5.52 S3)
- Дойти до конца записи → под «Вы записаны» inline-кнопок НЕТ
- Клавиатуры шагов гаснут при переходе к следующему шагу
- «❌ Отмена» на экране подтверждения — работает
- /cancel из подтверждения — ✅/❌ гаснут на summary
- /cancel НА ШАГЕ УСЛУГИ/ИМЕНИ — реально отменяет, а не превращается в подсказку/имя (5.52 W1)
- Любой текст БЕЗ активной записи (State None) → «Начните запись через /book» (5.52 SkipHandler fix)
- «Нет свободных дат/слотов» кнопка — не крутит спиннер вечно (5.52 S5)

### Background из старых сессий

- Передача бота Екатерине: поменять `ADMIN_ID` в `.env` на VPS (сейчас ID владельца — он сам тестирует). On-behalf booking (записывать клиентов по телефону из админки) — когда Екатерина начнёт работать
- Диагностика сети 5.51 (историч.): обрывы Errno 104 пачками, 13 раз за 2 дня; после включения воркера — мониторить `docker logs | grep -ciE 'reset|timeout'` и сравнивать

## Как деплоить

```bash
BARBER_PASS=$(grep -E '^PASS:' ~/.config/opencode/references/barber-bot-deploy-credentials.md | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh root@VPS_HOST_FROM_CRED_FILE
cd /opt/barber-bot && git pull && docker compose up -d --build
docker logs --tail 20 barber-bot-bot-1   # smoke-check: "Run polling for bot @My_Barber_hair_bot"
```

Миграции применяются автоматически (контейнер стартует через `alembic upgrade head && python -m bot.main`).

## Правила сессии (напоминание)

- **MY-VIBE-RULES.md** — dev-режим: deep-analysis на нетривиальное → реализация → verify (pytest/ruff/mypy) → code-review subagent → коммит свободный (личный репо)
- **Креды VPS НЕ коммитить** — живут в `~/.config/opencode/references/barber-bot-deploy-credentials.md`
- **VPS-диагностику делать самому** через sshpass (не просить юзера вводить команды)
- Code-reviewer subagent: если вернул пустой результат 2+ раза — REVIEW_UNAVAILABLE, фиксировать в коммит-месседже, детерминированные проверки делать самому
