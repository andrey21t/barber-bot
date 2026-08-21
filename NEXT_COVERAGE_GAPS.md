# NEXT_COVERAGE_GAPS.md — остатки после сессии 2026-08-21 (T7+T8 closed)

> Продолжение AUTONOMOUS_COVERAGE_PROMPT.md. Сессия закрыта на T7+T8.
> TOTAL coverage: 60% → 90% (T1-T6) → 91% (T7+T8). 191 тест проходит.

---

## Что покрыто в этой сессии (T7+T8)

| Задача | Файл | До | После | Тестов |
|---|---|---|---|---|
| T7 | bot/services/slots.py:26, 48-51 | 92% | 100% | 2 (test_slots.py extend) |
| T8 | bot/services/booking.py (8 веток) | 89% | 96% | 8 + 1 skipped (test_booking.py extend) |

**Итог TOTAL:** 1280 stmts, 134 miss (90%) → 112 miss (91%).
**Коммиты:** 709b12d (T7), 118e2e4 (T8), merge 6586392.

### T7 (slots.py 92% → 100%)
- `test_add_slots_empty_hours_raises_value_error` — line 26 (`if not hours: raise ValueError`).
- `test_add_slots_concurrent_race_raises_slot_exists` — lines 48-51 (IntegrityError → SlotAlreadyExistsError) via asyncio.gather on session_factory_concurrent.
- Code-reviewer (LGTM): 3 warnings (sqlite timeout hidden dep, LOW flakiness risk, docstring updated), 4 suggestions (S4 — production bug в error message slots.py:52: `hour` = loop variable last value, recorded ниже как B2).

### T8 (booking.py 89% → 96%)
8 новых тестов:
- `test_create_booking_service_id_random_not_in_db` — lines 98-100 (_select_service returns None для unknown service_id, fallback default duration).
- `test_create_booking_notification_log_idempotency_integrity_error` — lines 212-214 (NotificationLog IntegrityError SAVEPOINT idempotency, monkey-patch flush).
- `test_cancel_booking_notification_log_idempotency_integrity_error` — lines 372-374 (mirror for cancel).
- `test_create_booking_concurrent_race_integrity_error` — lines 180-182 (UNIQUE slot_id IntegrityError, stale DETACHED slot workaround для MissingGreenlet).
- `test_create_booking_slot_status_changed_between_select_and_update` — lines 195-196 (UPDATE slot rowcount=0, stale DETACHED slot).
- `test_cancel_booking_concurrent_race_booking_already_cancelled` — lines 353-354 (UPDATE booking rowcount=0).
- `test_transfer_booking_concurrent_cancel_race_raises_already_cancelled` — line 601 (recheck status='cancelled' after UPDATE rowcount=0).
- `test_transfer_booking_concurrent_new_slot_taken_raises_slot_already_booked` — lines 627-628 (UPDATE new_slot rowcount=0).

**1 skipped:** `test_create_booking_client_race_integrity_error` (lines 111-123) — production bug B1.

---

## Production bugs, найденные через тесты (НЕ фикшены)

### B1 — slot.id после rollback в _select_or_create_client race path
**Файл:** `bot/services/booking.py:166` (create_booking).
**Симптом:** `MissingGreenlet: greenlet_spawn has not been called` при доступе `slot.id` на attached Slot instance после `session.rollback()` внутри `_select_or_create_client` (lines 111-123, IntegrityError catch на concurrent client INSERT).
**Репро:** `pytest tests/test_booking.py::test_create_booking_client_race_integrity_error` (skipped).
**Причина:** после `await session.rollback()` (line 120) SQLAlchemy expires все атрибуты attached instances. `slot` был получен через `_select_open_slot` (line 149) и attached к session. Доступ `slot.id` на line 166 (Booking constructor) триггерит lazy load → MissingGreenlet (sync access к async session).
**Fix (предлагаемый):** capture `slot_id = slot.id` перед `_select_or_create_client` call (line 163), использовать `slot_id` в Booking constructor. Mirror: capture `slot.id` сразу после `_select_open_slot` и хранить в local var через весь flow.
**Severity:** Medium. Репро только при concurrent client INSERT race (редкий сценарий, но возможен на production при двойном tap newUser). На dev/одиночных запросах не проявляется (race не срабатывает).
**Тест:** skip-marked с описанием. После fix — убрать `@pytest.mark.skip`.

### B2 — wrong hour в SlotAlreadyExistsError message
**Файл:** `bot/services/slots.py:52`.
**Симптом:** error message `f"Slot {master_id}/{slot_date}/{hour} already exists"` использует `hour` из последней итерации цикла (line 38-43), не тот час, на котором упал IntegrityError.
**Пример:** `add_slots(session, master_id, date, [14, 15])` с конфликтом на hour=14 → message говорит ".../15 already exists" (неправильный час).
**Fix:** capture conflicting hour перед `flush` или найти его из exc (SQLAlchemy IntegrityError несёт info о нарушенной constraint). Проще: поменять message на `"Slot {master_id}/{slot_date} — one of hours {hours} already exists"` (без конкретного часа) — точнее отражает семантику.
**Severity:** Low. Не ломает UX (пользователь видит общее сообщение об ошибке).
**Тест:** covered indirectly через `test_add_slots_concurrent_race_raises_slot_exists` (hours=[14], одна итерация, баг не проявляется).

---

## Остатки — что делать в следующей сессии

### Tier 1 — Production bug fixes (поднимут coverage 91% → ~92%)

**B1 fix** (~15 мин) — capture slot.id перед _select_or_create_client:
```python
# bot/services/booking.py:149
slot = await _select_open_slot(session, payload.slot_id)
slot_id = slot.id  # capture BEFORE any rollback (B1 fix)
# ... использовать slot_id в Booking constructor (line 166)
booking = Booking(slot_id=slot_id, ...)
```
После fix: убрать `@pytest.mark.skip` с `test_create_booking_client_race_integrity_error` (booking.py 96% → 100%).

**B2 fix** (~5 мин) — generalise error message:
```python
# bot/services/slots.py:51-52
raise SlotAlreadyExistsError(
    f"Slot {master_id}/{slot_date} — one of hours already exists"
) from exc
```

### Tier 2 — NICE-TO-HAVE (низкий приоритет, не поднимут TOTAL значительно)

- `bot/main.py:16-112` (44 stmts, 0%) — bootstrap (bot init, dispatcher, on_startup). Mock aiogram Dispatcher + router wiring → сложно, отдельно.
- `bot/session.py:1-40` (24 stmts, 0%) — Telegram session init (aiogram internals). Низкая ценность.
- `bot/db.py:22-23, 28-29, 33, 43` (6 stmts) — `create_all`, `drop_all`, `dispose`, `utcnow`. dev/test утилиты, в prod не вызываются.
- `bot/handlers/client.py:293-297` (business_not_found в confirm_cb) — skipped в T5, symmetric to master_not_found, нужна FK violation (db surgery).
- `bot/handlers/client.py:406, 501-502, 525-526, 555, 588-589, 612-613, 741-742` (17 stmts) — mybookings/transfer ветки, частично для следующей итерации.
- `bot/handlers/admin.py:66, 128-129, 133, 136-137, 210, 242, 345` (9 stmts) — admin edge cases.
- `bot/keyboards/client.py:102-103, 124` (3 stmts) — keyboard edge cases.

### TOTAL ceiling
- Текущий: 91% (112 miss).
- После B1 fix (убрать skip → booking.py 100%): ~92% (103 miss).
- Реалистичный потолок без main.py/session.py: ~94% (64 miss).
- С main.py/session.py: ~99% (нужно mock aiogram internals, отдельная сессия).

---

## Промт на следующую сессию

```markdown
# AUTONOMOUS COVERAGE SESSION — barber-bot B1+B2 fix + final sweep

> Продолжение NEXT_COVERAGE_GAPS.md. Закрыть production bugs B1+B2,
> поднять coverage 91% → ~92%. После — опционально Tier 2 (admin/client
> handler edges) до ~94%.

## Контекст

**Репо:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно).
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev), APScheduler 3.x, freezegun.
**Формат:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (код сразу, без педагогики),
резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review.
**Последний commit:** `6586392` (merge tests/coverage-sweep-t7-t8, coverage 90% → 91%).
**Coverage baseline:** TOTAL 1280 stmts, 112 miss, 91% cover.

## Гит-стратегия

Новая ветка:
```bash
cd ~/PycharmProjects/barber-bot
git checkout -b fix/production-bugs-b1-b2
```
Коммитить свободно (личный репо, AGENTS.md § git-repo-categories). Одна задача = один коммит.
В конце — merge в main.

## B1 — slot.id после rollback в _select_or_create_client race path (~15 мин) — ПРИОРИТЕТ

**Файл:** `bot/services/booking.py:166` (create_booking).
**Тест:** `tests/test_booking.py::test_create_booking_client_race_integrity_error` (skipped, убрать skip после fix).

**Fix:** capture `slot_id = slot.id` сразу после `_select_open_slot` (line 149), использовать `slot_id` в Booking constructor (line 166) и в notification text (line 183, 187-190).

```python
# bot/services/booking.py:149
slot = await _select_open_slot(session, payload.slot_id)
slot_id = slot.id  # capture BEFORE any rollback (B1 fix)
business_tz = await _select_business_timezone(session, business_id)
start_at = _build_start_at(slot, business_tz)
# ... slot.id использовать НЕЛЬЗЯ после rollback (expired) — использовать slot_id
booking = Booking(slot_id=slot_id, ...)  # было slot.id
# ... в SlotAlreadyBooked error message (line 183) — slot_id, не slot.id
# ... в UPDATE slot WHERE clause (line 189) — slot_id, не slot.id
```

**Гейты:**
1. **deep-analysis** Pass 1+2 (state transition: rollback expires attributes).
2. Убрать `@pytest.mark.skip` с `test_create_booking_client_race_integrity_error`.
3. **qa-verify-and-fix**: `pytest && ruff check . && mypy bot scheduler.py tests`. Все зелёные.
4. **qa-code-review** subagent (logic-change + race fix — high-stakes).
5. **Commit:** `fix(booking): capture slot.id before _select_or_create_client (B1)`.

## B2 — wrong hour в SlotAlreadyExistsError message (~5 мин)

**Файл:** `bot/services/slots.py:52`.
**Fix:** generalise error message (hour из loop variable unreliable после IntegrityError):
```python
# bot/services/slots.py:51-52
raise SlotAlreadyExistsError(
    f"Slot {master_id}/{slot_date} — one of hours already exists"
) from exc
```

**Гейты:** deep-analysis trivial (rename message, no behavior change), qa-verify-and-fix.
**Commit:** `fix(slots): generalise SlotAlreadyExistsError message (B2)`.

## После B1+B2 (опционально, Tier 2)

Если осталось время и желание — Tier 2 из NEXT_COVERAGE_GAPS.md:
- `bot/handlers/admin.py:66, 128-129, 133, 136-137, 210, 242, 345` (9 stmts).
- `bot/handlers/client.py:293-297, 406, 501-502, 525-526, 555, 588-589, 612-613, 741-742` (17 stmts).
- `bot/keyboards/client.py:102-103, 124` (3 stmts).

Каждый edge case — отдельный тест, 1 edge = 1 stmt cover.

## Что НЕ делать

- ❌ Postgres migration (high-stakes — отдельная сессия с deep-analysis-critic).
- ❌ main.py / session.py / db.py coverage (низкий приоритет, mock aiogram internals).
- ❌ Refactoring — только fix B1+B2 + тесты.

## Гейты на КАЖДУЮ задачу (MANDATORY)

1. **deep-analysis** Pass 1+2 на нетривиальное (B1 — state transition, high-stakes).
2. **qa-verify-and-fix**: `pytest && ruff check . && mypy bot scheduler.py tests`.
3. **qa-code-review** subagent на logic-change (B1 — race fix, high-stakes).
4. **Commit** = конец логической единицы работы.

## Финальный отчёт

В конце сессии:
1. `git checkout main && git merge --no-ff fix/production-bugs-b1-b2`.
2. Обновить `NEXT_COVERAGE_GAPS.md` — что пофикшено, что осталось.
3. Резюме (5-10 строк): коммиты, coverage до/после, блокеры.

**Старт:** Прочитай промт целиком. Создай ветку. Начни с B1 (high-stakes — deep-analysis
Pass 1+2 обязательны). После B1+B2 — опционально Tier 2. В конце — merge + отчёт.
```

---

## Итог сессии (резюме)

**Что сделали:**
- T7 (slots.py 92% → 100%, 2 теста) — ValueError + SlotAlreadyExistsError race.
- T8 (booking.py 89% → 96%, 8 тестов + 1 skipped) — 8 error branches покрыты, B1 production bug найден и записан.
- TOTAL coverage: 90% → 91% (1280 stmts, 134 miss → 112 miss).
- 191 тест проходит, 2 skipped (B1 + FK violation edge).
- Все гейты зелёные: ruff + mypy + pytest. Code-reviewer на T7 (LGTM), T8 (verify через mypy/ruff/pytest + skip-marked для B1).

**Зачем продукту:** закрыли последние race conditions в services (slots concurrent insert, booking UPDATE rowcount=0 race paths, transfer recheck status='cancelled'). Booking race paths — критичные для double-click / concurrent cancel+transfer UX.

**Базовая вещь:** B1 production bug — `slot.id` на expired instance после rollback. Не проявляется на dev/одиночных запросах, но в race на production даёт MissingGreenlet. Fix тривиальный (capture slot_id перед rollback-prone call), но требует осторожности (deep-analysis high-stakes).

**Что НЕ сделали:** B1+B2 fix (промт на следующую сессию выше). Tier 2 (admin/client handler edges, 29 stmts, ~2% coverage gain).
