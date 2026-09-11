# NEXT_SESSION_PROMPT — barber-bot, session 5.59

## Контекст

Продолжаем работу над барбер-ботом (~/PycharmProjects/barber-bot). Прочитай
NEXT_SESSION_PROMPT.md в корне репо — там handoff после 5.58 (pre-flight +
scheduler coverage).

**main:** `4a030b9` (5.58: 6 тестов scheduler.py edge cases, 89%→98%)
**VPS:** задеплоен `d2734ef` в 16:33 UTC 11 сен (variant B). Коммиты `afc1f4d`
(5.57 тесты) и `4a030b9` (5.58 тесты) НЕ на проде — тесты не влияют на runtime,
деплой не нужен.
**Гейты:** ruff ✅ · mypy ✅ · pytest 560 passed / 2 skipped

## Что сделано в 5.58

### 1. Pre-flight проверка VPS ✅

Все live-тесты заблокированы временем (ближайший — backup 12.09 06:30 MSK).
Pre-flight подтверждает готовность:

- **VPS доступен**, контейнер свежий (built 11.09 16:33 UTC, up 4 мин на момент
  проверки)
- **Git HEAD на VPS:** `d2734ef` (variant B 5.57), `92dcada` (NEW-reminders 5.56)
  в истории → оба фикс-коммита на проде
- **NEW-reminders код в контейнере** (verified via `docker exec ... grep`):
  scheduler.py:191 — `f"Напоминаю: завтра в {time_str} — 💇 {service}, мастер {master_name}"`
  scheduler.py:193 — `f"Через час в {time_str} — 💇 {service}, мастер {master_name}"`
- **Variant B код в контейнере** (verified):
  client.py:237-238 — `if _is_master(message): await message.answer("📋 Меню:", reply_markup=admin_inline_menu())`
- **7 apscheduler jobs** совпадают с расписанием (verified, конвертированы в MSK):

  | Job ID | Next run (MSK) | Booking | Услуга |
  |---|---|---|---|
  | remind_1h_283ff705 | 12.09 12:00 | 283ff705 | Окрашивание 12.09 13:00 MSK |
  | remind_24h_1481976a | 12.09 12:30 | 1481976a | Мелирование 13.09 12:30 MSK |
  | remind_1h_a95de5b7 | 12.09 14:30 | a95de5b7 | Мелирование 12.09 15:30 MSK |
  | remind_24h_c97a5833 | 12.09 15:30 | c97a5833 | под ноль 13.09 15:30 MSK |
  | remind_1h_aff076c5 | 12.09 16:30 | aff076c5 | Окрашивание 12.09 17:30 MSK |
  | remind_1h_1481976a | 13.09 11:30 | 1481976a | Мелирование 13.09 12:30 MSK |
  | remind_1h_c97a5833 | 13.09 14:30 | c97a5833 | под ноль 13.09 15:30 MSK |

- **Cron backup готов:** `30 3 * * * /opt/barber-bot/scripts/backup.sh >> /var/log/barber_backup.log 2>&1`
  `/var/log/barber_backup.log` пока НЕ существует (ожидаемо — первый запуск
  12.09 03:30 UTC = 06:30 MSK)
- **Backups dir:** `/opt/barber-bot/backups/barber_2026-09-11_1007.dump` (26KB, ручной)
- **Mac offsite:** `~/barber-bot-backups/barber_2026-09-11_1007.dump` (синхронизирован
  через launchd `com.barber-offsite-backup`, exit code 0)

### 2. Coverage gaps — scheduler.py ✅

scheduler.py 89.1% → **98%** (108/110). 6 новых тестов (коммит `4a030b9`, запушен):

- `test_send_reminder_remind_1h_text` — remind_1h text branch (lines 192-193),
  critical path (каждый booking получает remind_1h)
- `test_send_reminder_unknown_kind_skips` — defensive else branch (lines 195-196),
  returns before log_notification → не портит UNIQUE guard
- `test_send_reminder_retry_after_exhausted` — оба attempt'а с RetryAfter (lines 217-222),
  sleep once + log error + return
- `test_send_reminder_generic_telegram_api_error` — base TelegramAPIError (lines 231-232, 238),
  network error — single attempt, no retry
- `test_schedule_for_booking_skips_both_past_due` — booking полностью в прошлом (line 297),
  оба reminder'а skipped (bot offline >24h scenario)
- `test_set_bot_ref_sets_global` — regression guard для _set_bot_ref (line 51)

Остались 2 statements (lines 66-73) — Postgres SQLAlchemyJobStore branch, нужен
реальный Postgres, пропущено по risk-priority.

**Общий coverage:** 78% → 79%. admin.py 65% (low priority, single-user),
client.py 79% (transfer_slot_30_cb covered в 5.57, error branches low priority).

## Задачи на сессию 5.59

### 1. Live-тест NEW-формата напоминаний (12.09 после 12:00 MSK)

Ближайшее: `remind_1h_283ff705` в 12.09 12:00 MSK — напоминание для Окрашивание
13:00 MSK (booking 283ff705, client Olesya tg=1156374642). Ожидаемый текст:
"Через час в 13:00 — 💇 Окрашивание, мастер Ekaterina"

**Проверить 12.09 после 12:00 MSK:**
```bash
CRED=~/.config/opencode/references/barber-bot-deploy-credentials.md
VPS_HOST=$(grep -E '^HOST:' $CRED | sed 's/^HOST: //')
BARBER_PASS=$(grep -E '^PASS:' $CRED | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
  -o PreferredAuthentications=password -o PubkeyAuthentication=no \
  root@$VPS_HOST 'docker exec barber-bot-db-1 psql -U barber -d barber -c \
  "SELECT id, booking_id, kind, sent_at, sent_at IS NOT NULL as sent FROM notifications_log WHERE sent_at > '"'"'2026-09-12 00:00'"'"' ORDER BY sent_at DESC LIMIT 10;"'
```

Если в notifications_log есть запись с kind='remind_1h' для booking 283ff705
и sent_at IS NOT NULL → ✅ live-тест пройден. Чтобы проверить текст сообщения
(NEW vs OLD формат), смотреть docker logs:
```bash
docker logs --since "2026-09-12T09:00:00Z" barber-bot-bot-1 2>&1 | grep -A 2 "send_reminder"
```

### 2. Live-тест variant B (после того как Ekaterina сделает /book confirm)

Ekaterina делает `/book` → выбирает дату/слот/услугу → confirm → должна видеть
"📋 Меню:" с 7 admin-кнопками. Если видит пустой чат — variant B не работает.

Альтернатива (быстрее): попросить Ekaterina сделать `/start` — должна увидеть
admin menu + stale reply keyboard убрана (ReplyKeyboardRemove).

### 3. Проверка ночного backup (12.09 после 06:30 MSK)

```bash
# VPS — после 12.09 06:30 MSK
CRED=~/.config/opencode/references/barber-bot-deploy-credentials.md
VPS_HOST=$(grep -E '^HOST:' $CRED | sed 's/^HOST: //')
BARBER_PASS=$(grep -E '^PASS:' $CRED | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh ... root@$VPS_HOST 'cat /var/log/barber_backup.log && ls -la /opt/barber-bot/backups/'

# Mac — после 12.09 ~06:35 MSK (launchd тянет с VPS)
ls -la ~/barber-bot-backups/ && find ~/barber-bot-backups/ -name "barber_2026-09-12*"
```

Если `/var/log/barber_backup.log` существует и содержит "OK: ... (verified TOC)"
— ночной backup работает. Если файла нет — cron не отработал, проверить
`journalctl --since "12 hours ago" | grep backup.sh`.

### 4. Coverage gaps (опционально, low priority)

По risk-priority — остальные gaps low priority:
- `bot/handlers/admin.py` 65% (644 строки) — single-user, ты сам админ
- `bot/handlers/client.py` 79% — error branches transfer_slot_30_cb (10 exception'ов),
  mirror transfer_slot_cb error tests (#16-25) — same service layer
- `scheduler.py` 98% — только Postgres branch (lines 66-73), нужен реальный Postgres

Принцип: покрываем по risk-priority, не ради 100%. Critical path (бронирование,
напоминания) — covered. Остальное — по настроению.

### 5. APScheduler orphan cleanup (future task, опционально)

7 active job'ов + orphan-записи от выполненных DateTrigger'ов НЕ чистятся
APScheduler'ом из PostgreSQL jobstore. Не критично, но можно добавить cleanup
в `on_startup_scan`. Низкий priority.

## Данные для live-тестов

### apscheduler_jobs (7 active, verified 11.09 19:39 MSK)

Таблица в разделе "Что сделано в 5.58" выше — times verified via Unix timestamp
конвертация.

### bookings (upcoming, на 11.09 16:33 UTC)

```
1481976a | 2026-09-13 09:30 UTC | Мелирование | client_tg=1156374642 (Olesya)
aff076c5 | 2026-09-12 14:30 UTC | Окрашивание  | client_tg=1156374642 (Olesya)
a95de5b7 | 2026-09-12 12:30 UTC | Мелирование  | client_tg=1156374642 (Olesya)
283ff705 | 2026-09-12 10:00 UTC | Окрашивание  | client_tg=1156374642 (Olesya)
47bf8c59 | 2026-09-11 12:30 UTC | Окрашивание и стрижка | client_tg=461355056 (Ekaterina!)
c97a5833 | 2026-09-13 12:30 UTC | под ноль     | client_tg=213896615
```

## Как деплоить (если понадобится)

Коммиты `afc1f4d` (5.57 тесты) и `4a030b9` (5.58 тесты) НЕ на проде — тесты
не влияют на runtime, деплой не нужен. Но если будет runtime-коммит:

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
