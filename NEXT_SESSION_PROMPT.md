# NEXT_SESSION_PROMPT — barber-bot тесты cancel_booking + Блок 3 часть 3 (перенос)

> Дата: 2026-08-21 · Сессия: завершена после self-review fix бага naive datetime в handler
> Цель следующей сессии: сначала — покрыть cancel_booking тестами (handler + service edge cases), потом — Блок 3 часть 3 (перенос записи)

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md` — читай перед каждой задачей, если код расходится со spec — прав spec, не код
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (без педагогики Уровня 2), резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Приоритет 1 — покрыть cancel_booking тестами (выявленные пробелы из self-review)

**Контекст:** при self-review пользователь спросил "у нас с тестами покрыт этот функционал???" — ответ был "частично": service покрыт (4 теста), handlers/keyboard/edge cases — НЕТ. Bug в `mybookings_msg:441` (naive vs aware datetime) поймался только self-review, не тестами. Это сигнал — display-математика в handler ≠ pure I/O, exemption по anti-overengineering правило 3 НЕ работает.

**Что покрыть (~6-8 тестов, ориентир 30-40 мин):**

### Service edge cases (tests/test_booking.py)
1. **`test_cancel_booking_not_found`** — booking_id не существует (random uuid4) → `BookingNotFoundError`. Сейчас покрыт только "чужой client_id". Различие: "чужой" должен идти через `WHERE client_id=?` (defense-in-depth), "не существует" — `WHERE id=?` возвращает None.
2. **`test_cancel_booking_transferred_status`** — booking.status='transferred' (NOT 'confirmed') → cancel должен пройти (UPDATE WHERE status IN ('confirmed', 'transferred')). Сейчас покрыт только confirmed.
3. **`test_cancel_booking_now_utc_default`** — `now_utc=None` (production path, default `datetime.now(UTC)`). Может потребоваться slot далеко в будущем (>24h от now), чтобы не упасть с CancelTooLateError.

### Handler tests (tests/test_client_handlers.py — НОВЫЙ файл, pattern из conftest.py mock_bot + dp.feed_update)
4. **`test_mybookings_msg_with_cancelable_booking`** — seed booking завтра 14:00, вызов `/mybookings`, проверить что в ответе есть inline-кнопка `Отменить`. Этот тест поймал бы naive-datetime баг из self-review.
5. **`test_mybookings_msg_with_too_late_booking`** — seed booking сегодня через 1 час, `/mybookings`, проверить что в ответе есть `⏰ Отмена недоступна`, НЕТ кнопки.
6. **`test_mybookings_msg_no_bookings`** — клиент без записей → "У вас нет активных записей. /book чтобы записаться"
7. **`test_mybookings_cancel_cb_happy_path`** — callback `mybook_cancel:<booking_id>`, проверить что booking.status='cancelled' после callback, мастеру отправлено сообщение "Отмена:".
8. **`test_mybookings_cancel_cb_too_late`** — slot сегодня (статус confirmed), callback → `CancelTooLateError` → ответ "❌ Отмена возможна только за 24+ часов до записи"
9. **`test_mybookings_cancel_cb_not_owner`** — stranger client_id → "Запись не найдена" (callback.answer с этим текстом)

### Keyboard test (можно в test_client_handlers.py или новый test_keyboards.py)
10. **`test_mybookings_keyboard_buttons`** — передать 2 booking → 2 кнопки в markup, callback_data корректный `mybook_cancel:<uuid>`

### Интеграционный тест (опционально, если есть ресурс)
11. **`test_full_cancel_flow`** — create_booking → /mybookings (видит кнопку) → tap [Отменить] → /mybookings снова (пусто, "У вас нет активных записей")

**Pattern для handler тестов:** `conftest.py:mock_bot` уже есть (`AsyncMock(Bot) + dp.feed_update(bot, update)`). Глянь `tests/test_admin.py` для handler-теста pattern если есть, или используй mock_bot + вручную вызвать `mybookings_msg(message=mock_message)` с mock message.

**Сложность:** средняя. Handler тесты в aiogram 3.x требуют mock Message/CallbackQuery — нетривиально. Если застрянешь на моках — coverage priority: test #4 (catch naive datetime bug), #7 (cancel_cb happy path), #1 (not_found), #2 (transferred). Остальные можно опустить если времени не хватит.

**Гейты:** verify (pytest + ruff + mypy) → code-review (если logic change — но это тесты, можно main agent review).

## Приоритет 2 — Блок 3 часть 3 — ПЕРЕНОС записи клиентом

**Spec.md** (строки 41, 318, 408-409):
- `/mybookings` → inline-кнопка «Перенести» возле каждой записи (аналогично «Отменить», новый callback prefix `mybook_transfer`)
- Правило: `CANCEL_MIN_HOURS = 24` (config.py:23) — клиент может перенести только за 24+ часов до `start_at` (то же правило что и для отмены)
- При переносе:
  1. **FSM** для выбора новой даты (re-use date picker) → нового слота (re-use slot picker)
  2. UPDATE `Booking.status='transferred'` (НЕ cancel+new — UPDATE той же строки, spec.md 318) + `Booking.start_at` + `end_at` + `slot_id` (новый)
  3. UPDATE старого `Slot.status='open'` (освободить старый)
  4. UPDATE нового `Slot.status='booked'` (занять новый) — rowcount check (race protection)
  5. `scheduler.remove_job` для remind_24h + remind_1h (старые jobs)
  6. `scheduler.add_job` новые remind_24h + remind_1h с новым start_at (через `schedule_for_booking` с `replace_existing=True`)
  7. `NotificationLog` `master_transfer` (kind допускается models.py:178-182)
  8. Подтверждение клиенту: «✅ Запись перенесена на ...»

**Где делать:**
- `bot/services/booking.py` — новый метод `transfer_booking(session, booking_id, new_slot_id, client_id, scheduler, *, now_utc=None) -> TransferResult`
- `bot/handlers/client.py` — новые FSM states (или re-use BookingStates с доп. steps для transfer), callback handlers
- `bot/keyboards/client.py` — `MyBookingsTransferCallbackData(prefix="mybook_transfer")` + кнопка «Перенести» под каждой cancelable записью
- `tests/test_booking.py` — тесты на transfer_booking (happy path, <24ч отказ, не-владелец, slot занят, scheduler cleanup + add новые)

**Контракты (не нарушать) — заимствовать из cancel_booking + create_booking:**
- `state.clear()` BEFORE `event.answer` (race condition, MY-VIBE-RULES.md 24)
- HTML escape: `client_name_snapshot` + `service_title_snapshot` сохраняются (НЕ пере-escape — уже escaped в DB)
- Timezone: `start_at` UTC в DB (naive в SQLite), для `CANCEL_MIN_HOURS` сравнение в naive UTC (pattern `admin.py:155-156`, как в `cancel_booking:332-334`)
- SQLite race: `UPDATE slot SET status='booked' WHERE status='open'` + rowcount check (как в `create_booking:184-196`)
- NotificationLog UNIQUE(booking_id, kind) — `master_transfer` через SAVEPOINT (booking.py:198-212 pattern)
- Scheduler: `remove_jobs_for_booking` для старых jobs + `schedule_for_booking(scheduler, booking_id, new_start_at)` для новых (`replace_existing=True` идемпотентен)

### Pre-existing failures (НЕ трогать)

- **`test_scheduler.py::test_on_startup_scan_phase_2_reschedules_upcoming`** — **FLAKY** (зависит от времени суток, не регрессия). Тест создаёт "tomorrow 14:00 MSK = tomorrow 11:00 UTC", on_startup_scan смотрит на 25h вперёд. Если current time >= 10:00 UTC → tomorrow 11:00 через <25h → проходит. Если current time < 10:00 UTC → вне окна → падает. Не чинить в этой сессии — отдельный блок (mock now_utc в on_startup_scan или freeze_time).
- **W6/W7/S2** (отложено с Блока 2) — minor, при миграции на Postgres.
- **Handlers тесты** для master-команд — pure I/O, anti-overengineering правило 3. НО: если в handler есть display-логика (математика, partition, datetime сравнение) — кандидат на тест (урок из Блока 3 часть 2 бага).

## Quick start следующей сессии (prompt для opencode)

```
Продолжаем barber-bot — покрыть cancel_booking тестами (Приоритет 1), потом Блок 3 часть 3 перенос записи (Приоритет 2, если хватит ресурса).

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) — там полный контекст с приоритетами и списком тестов.

ПРИОРИТЕТ 1 — тесты (~30-40 мин, ~6-8 тестов):
- Service edge cases в tests/test_booking.py: not_found, transferred_status, now_utc_default
- Handler tests в новом tests/test_client_handlers.py (pattern: conftest.py mock_bot + dp.feed_update):
  mybookings_msg с cancelable/too-late/no-bookings, mybookings_cancel_cb happy/too_late/not_owner
- Test #4 (mybookings_msg with cancelable booking) — ОСОБЫЙ приоритет, поймал бы naive-datetime баг из self-review
- Гейты: pytest + ruff + mypy зелёные. Tests = не logic change, code-review опционален (main agent review).

ПРИОРИТЕТ 2 — Блок 3 часть 3 перенос (если останется ресурс):
- spec.md строки 41, 318, 408-409 — контракты переноса
- bot/services/booking.py — pattern cancel_booking (UPDATE + rowcount + NotificationLog SAVEPOINT + remove_jobs_for_booking). Transfer = cancel (для старого slot) + create (для нового slot) в одной транзакции.
- ⚠️ ВНИМАНИЕ: transfer FSM handler тоже будет делать partition как mybookings_msg — НЕ забыть naive datetime в handler (урок из self-review баги)
- Гейты: deep-analysis (Pass 1-4) → verify → code-review subagent

Если останется ресурс — почини pre-existing FLAKY test_scheduler (но это отдельный блок, не основная задача).
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — строки 41, 318, 408-409 — контракты переноса | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + (next: transfer FSM) | 543 |
| `bot/services/booking.py` | create_booking + cancel_booking (pattern для transfer) | 408 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + MyBookingsCancelCallbackData (next: +Transfer) | 159 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog с kind CHECK | 184 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | Pattern для тестов booking service (happy path, idempotency, errors) + cancel_booking pattern | 462 |
| `tests/test_admin.py` | Pattern для тестов на services (timezone edge cases, _utc_naive helper) | ~480 |

## Гейты (напоминание)

- **Deep-analysis** на transfer_booking (logic-change + scheduler side effects) — Pass 1-4, не skip
- **Verify-and-fix** перед «готово»: pytest + ruff + mypy — все зелёные
- **Code-review** subagent после verify (logic-change) — по AGENTS.md § write-actions-subagents правило 7
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса

## Состояние тестов на момент сохранения

```
pytest: 49 passed (full suite, no failures)
ruff: All checks passed
mypy: 30 source files, no issues
```

Note: `test_scheduler::test_on_startup_scan_phase_2_reschedules_upcoming` — FLAKY (time-of-day dependent), иногда падает. Не регрессия от cancel_booking.

## Pre-existing Warning (зафиксировано, не блокирующее)

`booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite (booking.py:380 в cancel_booking, booking.py:217 в create_booking) — интерпретирует naive как system-local TZ. Корректно на Render (UTC), баг проявится при миграции на Postgres с TIMESTAMP WITHOUT TZ на сервере вне UTC. Фикс — Урок 2.6 (Postgres migration).
