# NEXT_SESSION_PROMPT — barber-bot, session 5.57

## Контекст

Продолжаем работу над барбер-ботом (~/PycharmProjects/barber-bot). Прочитай
NEXT_SESSION_PROMPT.md в корне репо — там handoff после 5.56 (human-readable
reminders + F1/W2 fixes, force-push rewrite истории от IP-leak).

**main:** `875d0c7` (5.56: human-readable reminders + html.escape master.name +
newline-squash + pre-push hook с generic IP regex)
**VPS:** задеплоен `92dcada` в 14:59 UTC 11 сен. W2 fix (.replace("\n"," ")) в
`875d0c7` НЕ на проде (только .md + scheduler.py:186) — подхватится при
следующем docker compose up --build. Не критично (мастер "Ekaterina" без \n).
**Гейты:** ruff ✅ · mypy ✅ · pytest 551 passed / 2 skipped

## Задачи на сессию 5.57

### 1. Валидация скриншотов из прошлой сессии (ВАЖНО — проверь код против фактов)

В прошлой сессии (5.56) юзер прислал скриншоты bot @My_Barber_hair_bot.
Ассистент сопоставил с БД (notifications_log, apscheduler_jobs, bookings):

| Скриншот | Тип | Время отправки | Деплой 14:59 UTC | Вердикт |
|---|---|---|---|---|
| "Через час: 15:30" | remind_1h для 47bf8c59 | 11:30 UTC (14:30 MSK) | ДО деплоя | OLD формат ✓ |
| "Напоминаю: завтра в 15:30" | remind_24h для 47bf8c59 | 13:40 UTC (16:40 MSK) | ДО деплоя | OLD формат ✓ |
| "Новая запись: 13 сентября, 12:30, Olesya, Мелирование" | master_new для 1481976a | 14:18 UTC (17:18 MSK) | ДО деплоя | OLD (5.56 master_new не менял) |

Все 3 напоминания на скриншотах — старый формат (отправлены ДО деплоя нового
кода). **Первый NEW-формат придёт:**
- Сб 12.09 12:00 MSK: remind_1h "Через час в 13:00 — 💇 Окрашивание, мастер Ekaterina"
- Сб 12.09 12:30 MSK: remind_24h "Напоминаю: завтра в 12:30 — 💇 Мелирование, мастер Ekaterina"

**Задача:** проверить логи на проде — пришли ли NEW-формат напоминания.
```bash
sshpass -p "$BARBER_PASS" ssh ... root@VPS 'docker logs --since 24h barber-bot-bot-1 2>&1' | rg "send_reminder|executed"
```
И проверить notifications_log:
```sql
SELECT * FROM notifications_log WHERE sent_at > '2026-09-12 00:00' ORDER BY sent_at DESC;
```
Если NEW-формат пришёл — ✅ live-тест пройден. Если OLD-формат — проверить
что контейнер реально на коде 875d0c7 (docker inspect image → created date).

### 2. Admin reply keyboard — ГЛАВНАЯ задача сессии

**Проблема:** Ekaterina (telegram_id=461355056) — мастер/админ, но видит
reply keyboard "Записаться" / "Мои записи" как обычный клиент. При этом у
неё есть booking "Окрашивание и стрижка, Андрей" — она тестировала как
клиент в собственной системе.

**Вопрос:** должен ли бот определять что telegram_id=461355056 это
мастер/админ и показывать admin-кнопки ("Сегодня", "Записания",
"Настройки") вместо client-кнопок?

**Что изучить:**
- `bot/handlers/start.py` — как бот определяет admin vs client (по
  `settings.admin_id`? по `Master.telegram_id`?)
- `bot/handlers/client.py:216 _restore_reply_keyboard_async` — где
  вызывается, для кого
- `bot/keyboards/client.py` — reply keyboard "Записаться"/"Мои записи"
- `bot/keyboards/admin.py` — admin keyboard, когда показывается
- `bot/config.py` — есть ли `ADMIN_ID` в settings
- Если мастер и клиент — один и тот же telegram_id (Ekaterina тестировала
  как клиент), как бот должен различать контексты?

**Гипотезы для проверки:**
1. `ADMIN_ID` не задан в `.env` → бот не знает кто админ → все видят
   client keyboard
2. `ADMIN_ID` задан, но проверка только в `/start` / admin handlers, а
   reply keyboard восстанавливается для всех без проверки
3. `ADMIN_ID` = telegram_id Ekaterina, но она хочет И клиентский доступ
   (бронировать за себя) И admin-доступ — нужен гибридный режим

**Варианты решения (обсудить с юзером перед реализацией):**
- A: admin видит ТОЛЬКО admin-кнопки (не может бронировать как клиент)
- B: admin видит admin-кнопки + "Записаться" (гибрид)
- C: admin выбирает режим командой `/admin` / `/client` (переключатель)
- D: оставить как есть — admin использует `/today` `/week` команды без
  reply keyboard, reply keyboard только для клиентов (но тогда зачем
  она показывается admin?)

### 3. Live-тест reminder NEW-формата (если ещё не пришёл)

Если на момент сессии напоминания на NEW-формате ещё не пришли — создать
тестовую запись:
- Через бота: `/book` → дата завтра+2 дня → слот → услуга → подтвердить
  → wait для remind_24h (если старт через 25h) или remind_1h (если через 1h+)
- Или через БД напрямую (быстрее, но не проверяет UX-путь):
```sql
INSERT INTO bookings (...) VALUES (..., start_at=NOW()+interval '2 hours', ...);
-- Затем через python код: schedule_for_booking(booking_id)
```

### 4. Проверка ночного backup (если ещё не проверена)

Cron `30 3 * * *` на VPS + launchd 06:30 MSK на Mac.
```bash
# VPS
sshpass ... ssh root@VPS 'tail /var/log/barber_backup.log && ls -la /opt/barber-bot/backups/'
# Mac
ls -la ~/barber-bot-backups/ && tail ~/barber-bot-backups/launchd.log
```

### 5. Coverage gaps (опционально, если будет время)

Покрытие 78% total. Ключевые gaps:
- `bot/handlers/admin.py` 65% (644 строки) — admin-функции, low priority
  (single-user, ты сам админ)
- `bot/handlers/client.py` 75% (252 строки) — `transfer_slot_30_cb` 130
  строк полностью непокрыт
- `scheduler.py` 89% — edge-cases (TelegramAPIError handler, network errors)

Принцип: покрываем по risk-priority, не ради 100%. Critical path
(бронирование, напоминания) — обязательно. Admin — low priority. Dead
code (legacy slots fallback) — лучше удалить, не покрывать.

## Данные для расследования admin keyboard

### DB state (на 2026-09-11 15:50 UTC)

```
masters:
  id=eaee30b1..., name="Ekaterina", telegram_id=461355056

bookings (upcoming):
  1481976a | 2026-09-13 09:30 UTC | Мелирование | client_tg=1156374642 (Olesya)
  aff076c5 | 2026-09-12 14:30 UTC | Окрашивание  | client_tg=1156374642 (Olesya)
  a95de5b7 | 2026-09-12 12:30 UTC | Мелирование  | client_tg=1156374642 (Olesya)
  283ff705 | 2026-09-12 10:00 UTC | Окрашивание  | client_tg=1156374642 (Olesya)
  47bf8c59 | 2026-09-11 12:30 UTC | Окрашивание и стрижка | client_tg=461355056 (Ekaterina!)
  c97a5833 | 2026-09-13 12:30 UTC | под ноль     | client_tg=213896615

notifications_log (last 5):
  23 | aff076c5 remind_24h | 14:30 UTC
  22 | 1481976a master_new | 14:18 UTC
  21 | 47bf8c59 remind_24h | 13:40 UTC
  20 | a95de5b7 remind_24h | 12:30 UTC
  19 | 47bf8c59 remind_1h  | 11:30 UTC
```

Ekaterina (telegram_id=461355056) имеет booking 47bf8c59 как CLIENT.
Значит она тестировала бронирование через бот от своего имени. Это и
вызывает вопрос: она видит client reply keyboard потому что она клиент
в системе, или потому что бот не distinguishes admin от client?

### apscheduler_jobs (7 active)

Все job'ы — remind_1h или remind_24h для upcoming bookings. Оrphan-записи
от выполненных DateTrigger'ов НЕ чистятся APScheduler'ом из PostgreSQL
jobstore (мусор накапливается). Не критично, но можно добавить cleanup
в on_startup_scan (future task).

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
- Pre-push: IP regex теперь в hook'е — любые IP-адреса в коммитах заблокированы
- **Перед admin keyboardChanges** — обсудить с юзером вариант решения
  (A/B/C/D выше), не начинать реализацию без согласия
