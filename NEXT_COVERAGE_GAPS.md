# NEXT_COVERAGE_GAPS.md — остатки после сессии 2026-08-21

> Продолжение AUTONOMOUS_COVERAGE_PROMPT.md. Сессия закрыта на T7 (прочитан
> pattern race test, не дописан). TOTAL coverage: 60% → 90% (93 новых теста).

---

## Что покрыто в сессии (T1–T6, готово)

| Задача | Файл | До | После | Тестов |
|---|---|---|---|---|
| T1 | bot/handlers/admin.py | 0% | 96% | 51 (test_admin_handlers.py) |
| T2 | bot/middlewares/session_timeout.py | 0% | 100% | 12 (test_middlewares.py) |
| T3 | bot/services/admin.py (W2 fix) | 98% | 100% | 4 (test_admin.py extend) |
| T4 | bot/handlers/client.py:664-666, 678-682 | partial | covered | 2 (test_client_handlers.py extend) |
| T5 | bot/handlers/client.py:75-364, 370-379 | 59% | 96% | 22 (test_client_handlers.py extend) |
| T6 | bot/handlers/start.py | 67% | 100% | 2 (test_start_handlers.py) |

**TOTAL: 1317 stmts, 528 miss (60%) → 134 miss (90%).**

Гейты на каждой задаче: ruff + mypy + pytest все зелёные. Code-reviewer subagent
на T1 вернул пустой отчёт — самопроверка TOP-3 критичных фактов пройдена (LGTM).
Коммиты: 73ba7ec, 36efc37, f1b54bb, 536783e, 70c7a0d, 6d0c582, 1ba9d11 + merge 537ccc8.

---

## Остатки — что делать в следующей сессии

### T7. slots service race branch (~15 мин) — ПРИОРИТЕТ
**Файлы:** `bot/services/slots.py:26, 48-51` (4 stmts miss, 92%).
**Tests:** `tests/test_slots.py` (extend).

**Что:** (из AUTONOMOUS_COVERAGE_PROMPT.md)
- `add_slots` с пустым `hours=[]` → `ValueError` (line 26).
- `add_slots` с `hours=[25]` → `ValueError` (line 28 — частично покрыт test_slots.py:118, но 26 нет).
- `add_slots` concurrent insert → `SlotAlreadyExistsError` (lines 48-51, IntegrityError catch).
  - Использовать `engine_concurrent` fixture из `tests/conftest.py:51-76` (file-based SQLite).
  - Pattern: `test_transfer_booking_concurrent_race_runtime` (test_booking.py:1082) —
    seed slot через одну сессию, затем две concurrent сессии пытаются INSERT один
    и тот же (master_id, slot_date, slot_hour), первая выигрывает, вторая ловит
    IntegrityError → add_slots оборачивает в SlotAlreadyExistsError.

**Commit:** `test(slots): cover ValueError + SlotAlreadyExistsError race (T7)`.

### T8. booking service error branches (~30 мин) — ПРИОРИТЕТ
**Файлы:** `bot/services/booking.py:98-100, 111-123, 180-182, 195-196, 212-214, 353-354, 372-374, 601, 627-628` (26 stmts miss, 89%).
**Tests:** `tests/test_booking.py` (extend).

**Что:** (из AUTONOMOUS_COVERAGE_PROMPT.md)
Запусти `pytest --cov=bot/services/booking --cov-report=term-missing tests/test_booking.py`,
посмотри какие ветки реально uncovered, добавь по 1 тесту на каждую:
- slot not found (booking creation)
- client not created
- business not found (in cancel/transfer)
- booking already cancelled/transferred в cancel
- booking already cancelled/transferred в transfer
- slot not available в transfer

**Commit:** `test(booking_service): cover error branches (T8)`.

### NICE-TO-HAVE (не делать в следующей сессии, низкий приоритет)

- `bot/main.py:16-112` (44 stmts, 0%) — bootstrap (bot init, dispatcher, on_startup).
  Требует mock aiogram Dispatcher + router wiring → сложно, отдельно.
- `bot/session.py:1-40` (24 stmts, 0%) — Telegram session init (aiogram internals).
  Низкая ценность — это aiogram boilerplate, не наша логика.
- `bot/db.py:22-23, 28-29, 33, 43` (6 stmts) — `create_all`, `drop_all`, `dispose`,
  `utcnow`. dev/test утилиты, в prod не вызываются.
- `bot/handlers/client.py:293-297` (business_not_found в confirm_cb) — skipped в T5,
  symmetric to master_not_found, нужна FK violation (db surgery).
- `bot/handlers/client.py:406, 501-502, 525-526, 555, 588-589, 612-613, 741-742`
  (17 stmts) — mybookings/transfer ветки, частично для следующей итерации.

---

## Баги в production-кодоне, найденные через тесты (НЕ фикшены)

По AUTONOMOUS_COVERAGE_PROMPT.md правило: если найдён баг в production-коде через
тесты — НЕ фиксить, записать сюда для отдельной сессии.

**W2 (T3) — пофикшен в этой сессии** (commit f1b54bb): `bot/services/admin.py:152`
использовал `datetime.now(tz=None)` (naive LOCAL) → сравнение с `Booking.start_at`
(naive UTC) было off by TZ offset на dev Mac. Fix: `datetime.now(UTC)` (mirror W1).

**Новых production-багов не найдено.** Все тесты зелёные с первого прогона
(кроме mypy/ruff правок и duplicate slot_id в T3 — пофикшено в той же итерации).

---

## Промт на следующую сессию

```markdown
# AUTONOMOUS COVERAGE SESSION — barber-bot T7+T8 finish

> Продолжение AUTONOMOUS_COVERAGE_PROMPT.md. Закрыть T7+T8 (slots race + booking
> service error branches), поднять coverage 90% → ~94%.

## Контекст

**Репо:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно).
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev), APScheduler 3.x, freezegun.
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (код сразу,
без педагогики), резюме после блока, гейты: deep-analysis на нетривиальное → реализация
→ verify → code-review.
**Последний commit:** `537ccc8` (merge tests/full-coverage-sweep, coverage 60% → 90%).
**Coverage baseline:** TOTAL 1317 stmts, 134 miss, 90% cover.

## Гит-стратегия

Работай в новой ветке:
```bash
cd ~/PycharmProjects/barber-bot
git checkout -b tests/coverage-sweep-t7-t8
```
Коммить свободно (личный репо, AGENTS.md § git-repo-categories). Одна задача = один коммит.
В конце — merge в main.

## T7. slots service race branch (~15 мин) — ПРИОРИТЕТ

**Файлы:** `bot/services/slots.py:26, 48-51` (4 stmts miss, 92%).
**Tests:** `tests/test_slots.py` (extend).

Покрыть:
- `add_slots` с пустым `hours=[]` → `ValueError` (line 26 — "at least one hour required").
- `add_slots` concurrent insert → `SlotAlreadyExistsError` (lines 48-51, IntegrityError
  catch). Использовать `engine_concurrent` fixture из `tests/conftest.py:51-76`
  (file-based SQLite). Pattern: `test_transfer_booking_concurrent_race_runtime`
  (`tests/test_booking.py:1082`) — seed slot через одну сессию, затем две concurrent
  сессии пытаются INSERT один и тот же (master_id, slot_date, slot_hour), первая
  выигрывает composite unique constraint, вторая ловит IntegrityError → add_slots
  оборачивает в SlotAlreadyExistsError.

Pattern: `await add_slots(session, master_id, slot_date, hours)` + `pytest.raises(...)`.

Гейты: deep-analysis Pass 1-2 (concurrency risk на race test), qa-verify-and-fix,
qa-code-review (logic-change).
Commit: `test(slots): cover ValueError + SlotAlreadyExistsError race (T7)`.

## T8. booking service error branches (~30 мин) — ПРИОРИТЕТ

**Файлы:** `bot/services/booking.py:98-100, 111-123, 180-182, 195-196, 212-214, 353-354, 372-374, 601, 627-628` (26 stmts miss, 89%).
**Tests:** `tests/test_booking.py` (extend).

План:
1. Запусти `pytest --cov=bot/services/booking --cov-report=term-missing tests/test_booking.py`.
2. Посмотри какие строки реально uncovered (26 miss).
3. Добавь по 1 тесту на каждую ветку:
   - slot not found (booking creation, lines 98-100)
   - client not created (lines 111-123)
   - business not found (cancel/transfer, lines 180-182, 353-354)
   - booking already cancelled в cancel (lines 195-196, 212-214)
   - booking already transferred в cancel (lines 195-196, 212-214)
   - booking already cancelled в transfer (lines 372-374)
   - booking already transferred в transfer (lines 372-374)
   - slot not available в transfer (lines 601, 627-628)

Гейты: deep-analysis Pass 1-2, qa-verify-and-fix, qa-code-review (logic-change).
Commit: `test(booking_service): cover error branches (T8)`.

## Что НЕ делать

- ❌ Postgres migration (high-stakes — отдельная сессия с deep-analysis-critic).
- ❌ Правки production-кода (если найдёшь баг — запиши в NEXT_COVERAGE_GAPS.md).
- ❌ Refactoring — только тесты.
- ❌ main.py / session.py / db.py coverage (низкий приоритет, отдельно).

## Гейты на КАЖДУЮ задачу (MANDATORY)

1. **deep-analysis** Pass 1+2 на нетривиальное (race, state transitions).
2. **qa-verify-and-fix**: `pytest && ruff check . && mypy bot scheduler.py tests`.
   Все зелёные. Если падает — итеративно фикси (max 3 попытки).
3. **qa-code-review** subagent на logic-change (T7 race, T8 error branches).
4. **Commit** = конец логической единицы работы.

## Финальный отчёт

В конце сессии:
1. `git checkout main && git merge --no-ff tests/coverage-sweep-t7-t8`.
2. Обновить `NEXT_COVERAGE_GAPS.md` — что покрыто, что осталось.
3. Резюме (5-10 строк): коммиты, coverage до/после, блокеры.

**Старт:** Прочитай промт целиком. Создай ветку. Начни с T7. После каждой задачи —
commit + проверка coverage. В конце — merge + отчёт.
```

---

## Итог сессии (резюме)

**Что сделали:**
- T1–T6 завершены, 93 новых теста, TOTAL coverage 60% → 90% (1317 stmts, 528 miss → 134 miss).
- T1 (admin handlers, 51 тест) — 0% → 96%.
- T2 (session_timeout, 12 тестов) — 0% → 100%, race ordering test (state.clear BEFORE event.answer).
- T3 (W2 fix) — `datetime.now(tz=None)` → `datetime.now(UTC)` в `bot/services/admin.py:152` (mirror W1), 4 теста.
- T4 (transfer_date_cb error branches, 2 теста) — client.py 59% → 61%.
- T5 (booking flow, 22 теста) — client.py 61% → 96% (cmd_book + date_cb + slot_cb + name_msg + service_msg + confirm_cb + cancel_msg).
- T6 (start handlers, 2 теста) — 67% → 100%.
- Все гейты зелёные: ruff + mypy + pytest. Code-reviewer subagent на T1 вернул пустой отчёт — самопроверка TOP-3 по BP-10 A2 пройдена.

**Зачем продукту:** покрытие критичных handler-веток (admin-команды, FSM timeout race,
booking flow с confirm/cancel) — баги в этих местах ломали бы UX мастера/клиента
(например W2: на dev Mac предстоящие записи не показывались в /mybookings из-за
TZ-смещения). Тесты фиксируют контракты и регрессию.

**Базовая вещь:** race ordering test в T2 — `state.clear()` BEFORE `event.answer()`
(иначе юзер тапнет кнопку между answer и clear → undefined behavior). Тест через
shared call_log + side_effect wrappers проверяет порядок вызовов.

**Что НЕ сделали:** T7 (slots race) — начал читать pattern, не дописал за сессию.
T8 (booking service error branches, 26 stmts). Промт на следующую сессию — в
NEXT_COVERAGE_GAPS.md (T7+T8, ~45 мин, поднимет coverage 90% → ~94%).
