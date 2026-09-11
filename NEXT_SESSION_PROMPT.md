# NEXT_SESSION_PROMPT — barber-bot, session 5.58

## Контекст

Продолжаем работу над барбер-ботом (~/PycharmProjects/barber-bot). Прочитай
NEXT_SESSION_PROMPT.md в корне репо — там handoff после 5.57 (admin reply
keyboard variant B + coverage gaps closure).

**main:** `afc1f4d` (5.57: 2 теста transfer_slot_30_cb, 0%→covered)
**VPS:** задеплоен `d2734ef` в 16:33 UTC 11 сен (variant B — admin_inline_menu
для master в _restore_reply_keyboard_async). Коммит afc1f4d (тесты) НЕ на
проду — подхватится при следующем `docker compose up --build` (тесты не
влияют на runtime, деплой не срочный).
**Гейты:** ruff ✅ · mypy ✅ · pytest 554 passed / 2 skipped

## Что сделано в 5.57

### 1. Admin reply keyboard — variant B (ГЛАВНАЯ задача, закрыта) ✅

**Проблема:** Ekaterina (telegram_id=461355056, ADMIN_ID) видела stale client
reply keyboard "Записаться"/"Мои записи" с прошлой сессии (до B.13/5.36).
Код правильно запрещает показ (4 guard'а _is_master), но Telegram не убирает
уже показанную keyboard (`is_persistent=True`).

**Решение (variant B):** в `_restore_reply_keyboard_async` (client.py:237-239)
для master вместо silent `return` отправляется `admin_inline_menu()` (7 inline
кнопок, текст "📋 Меню:"). Master может `/book` (без guard, по дизайну) и после
confirm/cancel видит admin menu, не пустой чат.

**Файлы:**
- `bot/handlers/client.py:75` — импорт `admin_inline_menu`
- `bot/handlers/client.py:217-240` — helper с master branch
- `tests/test_client_handlers.py:5408+` — `test_restore_reply_keyboard_async_master_gets_admin_menu`
- Коммит `d2734ef`, запушен, задеплоен на VPS

**Code-review:** VERDICT LGTM (2 suggestions — S1 stale keyboard cleanup через
ReplyKeyboardRemove отклонено — разойдётся с `/menu` convention; S2
`assert_awaited_once` coupling — корректно, ловит будущие изменения).

**Что НЕ сделано:** stale reply keyboard не убирается автоматически — Ekaterina
нужно сделать `/start` (ReplyKeyboardRemove уберёт stale keyboard). Это
конгруэнтно `/menu` и `admin_menu_cb` — они тоже не шлют ReplyKeyboardRemove.
Если после `/start` keyboard снова появится — значит реальный баг.

### 2. Coverage gaps — transfer_slot_30_cb ✅

130 строк / 0% coverage → covered. 2 новых теста:
- `test_transfer_slot_30_cb_happy_path` — workday-path transfer, master
  notified, client gets "✅ Запись перенесена", booking.status='transferred'
- `test_transfer_slot_30_cb_invalid_start_minute` — start_minute=1500 (out
  of [0,1439]) → state.clear + "❌ Ошибка выбора времени" early return

Коммит `afc1f4d`, запушен. Деплой НЕ нужен — тесты не влияют на runtime.

### 3. Backup проверка ✅

- VPS: cron `30 3 * * *` установлен 11.09 10:08 UTC. `/var/log/barber_backup.log`
  ещё НЕ создан — первый автоматический ночной backup будет **12.09 03:30 UTC**
  (06:30 MSK). Dump в `/opt/barber-bot/backups/barber_2026-09-11_1007.dump` —
  ручной запуск перед cron'установкой.
- Mac: `~/barber-bot-backups/barber_2026-09-11_1007.dump` синхронизирован через
  launchd `com.barber-offsite-backup` (зарегистрирован в launchctl).

## Задачи на сессию 5.58

### 1. Live-тест NEW-формата напоминаний (БЛОКИРУЕТСЯ ВРЕМЕНЕМ — завтра)

На 11.09 16:33 UTC (момент деплоя d2734ef) ближайшие запланированные
напоминания:
- **12.09 09:00 UTC (12:00 MSK)** — `remind_1h` для 283ff705 (Окрашивание 13:00 MSK)
- **12.09 09:30 UTC (12:30 MSK)** — `remind_24h` для 1481976a (Мелирование 13.09 12:30 MSK)
- **12.09 11:30 UTC (14:30 MSK)** — `remind_1h` для a95de5b7 (Мелирование 12.09 15:30 MSK)

**Проверить 12.09 после 12:00 MSK:**
```bash
CRED=~/.config/opencode/references/barber-bot-deploy-credentials.md
VPS_HOST=$(grep -E '^HOST:' $CRED | sed 's/^HOST: //')
BARBER_PASS=$(grep -E '^PASS:' $CRED | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh ... root@$VPS_HOST 'docker exec barber-bot-db-1 psql -U barber -d barber -c "SELECT id, kind, sent_at, booking_id FROM notifications_log WHERE sent_at > '"'"'2026-09-12 00:00'"'"' ORDER BY sent_at DESC;"'
```

**NEW-формат (5.56):** "Через час в 13:00 — 💇 Окрашивание, мастер Ekaterina"
(с service name + master name). OLD-формат: "Через час: 15:30" (без названий).

Если NEW-формат пришёл — ✅ live-тест пройден. Если OLD-формат — проверить
что контейнер реально на коде 92dcada (docker inspect image → created date,
должно быть 11.09 ~14:58 UTC или позже).

### 2. Live-тест variant B (после 12.09, когда Ekaterina будет пользоваться)

Ekaterina делает `/book` → выбирает дату/слот/услугу → confirm → должна видеть
"📋 Меню:" с 7 admin-кнопками (Открыть день, Изменить окно, Сегодня, Неделя,
Открыть неделю, Закрыть день, Услуги). Если видит пустой чат — variant B не
работает, нужно дебажить.

Альтернатива: попросить Ekaterina сделать `/start` — должна увидеть admin
menu + stale reply keyboard убрана (ReplyKeyboardRemove).

### 3. Проверка ночного backup (12.09, после 06:30 MSK)

```bash
# VPS — после 12.09 06:30 MSK
sshpass ... ssh root@$VPS_HOST 'cat /var/log/barber_backup.log && ls -la /opt/barber-bot/backups/'
# Mac — после 12.09 ~06:35 MSK (launchd тянет с VPS)
ls -la ~/barber-bot-backups/ && find ~/barber-bot-backups/ -name "barber_2026-09-12*" -newer ~/barber-bot-backups/barber_2026-09-11_1007.dump
```

Если `/var/log/barber_backup.log` существует и содержит "OK: ... (verified
TOC)" — ночной backup работает. Если файла нет — cron не отработал, проверить
`journalctl --since "12 hours ago" | grep backup.sh`.

### 4. Coverage gaps (опционально, если будет время)

Покрытие 78% → 78.x% (после 5.57 transfer_slot_30_cb). Ключевые gaps:
- `bot/handlers/admin.py` 65% (644 строки) — admin-функции, low priority
  (single-user, ты сам админ)
- `bot/handlers/client.py` 75% → ~77% — `transfer_slot_30_cb` теперь covered,
  но error branches (10 exception'ов) НЕ покрыты. Mirror transfer_slot_cb
  error tests (#16-25) — низкий priority (same service layer)
- `scheduler.py` 89% — edge-cases (TelegramAPIError handler, network errors)

Принцип: покрываем по risk-priority, не ради 100%. Critical path
(бронирование, напоминания) — обязательно. Admin — low priority. Dead
code (legacy slots fallback) — лучше удалить, не покрывать.

### 5. APScheduler orphan cleanup (future task, опционально)

7 active job'ов + orphan-записи от выполненных DateTrigger'ов НЕ чистятся
APScheduler'ом из PostgreSQL jobstore (мусор накапливается). Не критично,
но можно добавить cleanup в `on_startup_scan`. Низкий priority.

## Данные для live-тестов

### apscheduler_jobs (7 active, на 11.09 16:33 UTC)

| Job ID | Next run (UTC) | Booking | Услуга |
|---|---|---|---|
| remind_1h_283ff705 | 12.09 09:00 | 283ff705 | Окрашивание 12.09 10:00 UTC |
| remind_24h_1481976a | 12.09 09:30 | 1481976a | Мелирование 13.09 09:30 UTC |
| remind_1h_a95de5b7 | 12.09 11:30 | a95de5b7 | Мелирование 12.09 12:30 UTC |
| remind_24h_c97a5833 | 12.09 12:30 | c97a5833 | под ноль 13.09 12:30 UTC |
| remind_1h_aff076c5 | 12.09 13:30 | aff076c5 | Окрашивание 12.09 14:30 UTC |
| remind_1h_1481976a | 13.09 08:30 | 1481976a | Мелирование 13.09 09:30 UTC |
| remind_1h_c97a5833 | 13.09 11:30 | c97a5833 | под ноль 13.09 12:30 UTC |

### bookings (upcoming, на 11.09 16:33 UTC)

```
1481976a | 2026-09-13 09:30 UTC | Мелирование | client_tg=1156374642 (Olesya)
aff076c5 | 2026-09-12 14:30 UTC | Окрашивание  | client_tg=1156374642 (Olesya)
a95de5b7 | 2026-09-12 12:30 UTC | Мелирование  | client_tg=1156374642 (Olesya)
283ff705 | 2026-09-12 10:00 UTC | Окрашивание  | client_tg=1156374642 (Olesya)
47bf8c59 | 2026-09-11 12:30 UTC | Окрашивание и стрижка | client_tg=461355056 (Ekaterina!)
c97a5833 | 2026-09-13 12:30 UTC | под ноль     | client_tg=213896615
```

## Как деплоить

```bash
CRED=~/.config/opencode/references/barber-bot-deploy-credentials.md
VPS_HOST=$(grep -E '^HOST:' $CRED | sed 's/^HOST: //')
BARBER_PASS=$(grep -E '^PASS:' $CRED | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
  -o PreferredAuthentications=password -o PubkeyAuthentication=no \
  root@$VPS_HOST 'cd /opt/barber-bot && git pull && docker compose up -d --build && \
  docker logs --tail 20 barber-bot-bot-1' 2>&1 | tail -30
```

## Правила сессии

- MY-VIBE-RULES.md — dev-режим: deep-analysis → реализация → verify →
  code-review → коммит свободный (личный репо)
- Креды VPS НЕ коммитить — в `~/.config/opencode/references/barber-bot-deploy-credentials.md`
- VPS-диагностику делать самому через sshpass
- Pre-push: IP regex в hook'е — любые IP-адреса в коммитах заблокированы
- **Перед новыми фичами** — обсудить с юзером, не начинать реализацию без согласия
- Coverage gaps — покрывать по risk-priority, не ради 100%
