# NEXT_SESSION_PROMPT — Session 5.21: шаг 5.8a миграция 006 + service branching.

> Дата: 2026-08-29 · **Session 5.20 завершена без коммитов (deep-analysis only)**: Pass 1-4 + critic iter 1 SURFACE_LEVEL + iter 2 DEEP_ENOUGH. Базовый уровень тестов: 326 passed + 2 skipped (предыдущая сессия 5.19).
> Prod остаётся на `c64b31d` (Session 5.9). Миграция 005 НЕ накатывалась на prod — smoke-test на dev-копии обязателен (one-way door, данные Екатерины).

## ⚡ TL;DR для Session 5.21

**Цель 5.21:** шаг **5.8a — миграция 006 (Booking.slot_id nullable + drop UNIQUE + drop FK) + BookingCreate contract + create_booking branching (workday path) + cascade в cancel/transfer + тесты**. Без handler cmd_slots (это 5.8b).

**Главные артефакты:** этот файл + `PLANS.md` (Session 5.20 в Decision Log + Plan of Work п.8).

**Risk-класс 5.21: HIGH-STAKES** (миграция — one-way door, cascade в 4 функциях, ~6 файлов). Deep-analysis Pass 1-4 + critic iter 2 уже сделаны в Session 5.20 — **VERDICT: DEEP_ENOUGH**. Можно сразу к impl (без повторного Pass 1-4 — другая сессия, та же задача).

## 5.8 разложен на 5.8a / 5.8b / 5.8c (Session 5.20 finding)

Общий scope 5.8: 11 файлов, ~745 строк — слишком много для одной сессии. Split:

| Подфаза | Scope | Файлы | Строк | Сессия |
|---|---|---|---|---|
| **5.8a** (эта сессия) | Migration 006 + models nullable + schemas contract + booking-service branching + tests | 6 + PLANS.md update | ~438 | 5.21 |
| 5.8b | cmd_slots handler + BookSlot30CallbackData + slot_picker_keyboard_30min real callback + mybookings_keyboard hide-transfer-for-WorkDay + service_msg/confirm_cb branching | 2 | ~180 | 5.22 |
| 5.8c | tests/test_slots_command.py (cmd_slots + slot_cb_30 + service_msg + confirm_cb workday) + test_mybookings_workday_no_transfer | 2 | ~340 | 5.22 или 5.23 |

## Critic iter 2 — 5 minor findings (A-E), 0 blockers

Прежде чем писать код — учесть 5 minor findings critic'а (всё grounded в file:line, верифицировано BP-10 A2):

- **A. CancelResult.slot_id** (`bot/services/booking.py:556`) — тоже `slot_id: UUID`, не Optional. Cancel workday-path (line 679 `slot_id=booking.slot_id=None`) упадёт на dataclass validation. **Fix: 1 строка** `slot_id: UUID | None`.
- **B. Schema contract** — `BookingCreate.start_at: datetime` НЕ соответствует `_build_start_at_from_workday(workday, start_time_local: time, business_tz)` (booking.py:289-290 — берёт LOCAL `time`, возвращает UTC datetime). UI /slots генерит LOCAL HH:MM → BookSlot30CallbackData несёт `start_time_local`. **Fix:** `start_time_local: time | None` в BookingCreate (НЕ `start_at: datetime`). Compile-time catch, не silent.
- **C. Migration 006 downgrade FK** — "synthetic UUID" не удовлетворит FK re-add (нет matching Slot row, в отличие от migration 004:46 где price=0 — scalar). **Fix:** downgrade = `DELETE FROM bookings WHERE slot_id IS NULL` перед re-add NOT NULL (pet-project single-tenant, data-loss acceptable — downgrade это rollback scenario).
- **D. Migration-test infra не существует** — `ls tests/ | grep migration` пусто, conftest.py:43 использует `Base.metadata.create_all` (НЕ alembic). test_migration_006.py (~60 строк) = NEW test infra с нуля (alembic.command.upgrade programmatic ИЛИ mock op). **Fallback:** если migration-test окажется сложным, отложить в 5.8b, ship 5.8a с migration-файлом + booking-тестами.
- **E. PLANS.md 006 drift** — line 89 "FK drop (006)" vs line 181/407 "006 drop table slots". Главный split на 006 (FK+nullable) + 007 (drop table) — sound, но PLANS.md не обновлён. **Fix:** update PLANS.md Decision Log (1 абзац). Документация, не код.

## 5.8a — Plan of Work (точный список для Session 5.21)

| # | Файл | Что | Строк |
|---|---|---|---|
| 1 | `alembic/versions/006_booking_slot_nullable.py` | New migration: `batch_alter_table("bookings")` для SQLite (nullable=True + drop_index ux_bookings_slot + drop FK). Postgres: explicit `DROP CONSTRAINT IF EXISTS fk_bookings_slot_id` перед batch. Downgrade: `DELETE FROM bookings WHERE slot_id IS NULL` → batch re-add NOT NULL + UNIQUE index + FK. Pattern: `alembic/versions/004_service_price_nullable.py:36-47`. | ~80 |
| 2 | `bot/models.py:179, 205` | `Booking.slot_id: Mapped[UUID | None] = mapped_column(..., nullable=True)`. Remove `Index("ux_bookings_slot", "slot_id", unique=True)` line. FK на slots остаётся (legacy slots table жива до migration 007). | ~3 |
| 3 | `bot/schemas.py:16-31` | `BookingCreate`: `slot_id: UUID | None = None`, `+ workday_id: UUID | None = None`, `+ start_time_local: time | None = None` (НЕ `start_at: datetime` per finding B). Pydantic validator: `slot_id XOR (workday_id AND start_time_local)`, else ValueError. `BookingOut.slot_id: UUID | None`. | ~25 |
| 4 | `bot/services/booking.py:80, 365-525, 590-700, 740-960` | create_booking branching: `if payload.slot_id:` legacy path (existing code). `elif payload.workday_id and payload.start_time_local:` workday path — SELECT WorkDay, `_validate_booking_within_workday` (existing), `_build_start_at_from_workday` (existing, takes `time`), `_check_multi_client_capacity` (existing), INSERT Booking (slot_id=None). `BookingCreatedData.slot_id: UUID | None` (line 84). `CancelResult.slot_id: UUID | None` (line 556, finding A). `cancel_booking`: `if booking.slot_id is not None: UPDATE Slot` (skip for workday). `transfer_booking`: `if booking.slot_id is None: raise NotImplementedError("WorkDay transfer is 5.9 scope — admin_move_booking")` after ownership check. | ~120 |
| 5 | `tests/test_booking.py:1602-1705` | UPDATE `test_create_booking_concurrent_race_integrity_error` line 1692 `match="UNIQUE constraint"` → `match="was taken/closed between SELECT and UPDATE"` (rowcount-check path after UNIQUE drop). Add 4 new tests: `test_create_booking_workday_path` (happy), `test_create_booking_workday_capacity_blocks_2nd`, `test_cancel_booking_workday_no_slot_update`, `test_transfer_booking_workday_raises_not_implemented`. | ~120 |
| 6 | `tests/test_migration_006.py` (new, optional fallback to 5.8b) | Migration tests: nullable column, UNIQUE drop, FK drop (SQLite batch verification), downgrade reversibility. NEW infra (conftest uses `create_all` not alembic). | ~60 |
| + | `PLANS.md` Decision Log + Session 5.20 section | Update Decision Log: 006 split на 006 (FK+nullable) + 007 (drop table slots). Add Session 5.20 (deep-analysis-only) + Gap 7 closure (UNIQUE guard-shift). | ~30 |

**Итого 5.8a: ~438 строк, 6+1 файлов.**

## Что НЕ в scope 5.8a (deferred to 5.8b/c)

- ❌ `bot/handlers/client.py` cmd_slots handler, slot_cb_30
- ❌ `bot/keyboards/client.py` BookSlot30CallbackData, slot_picker_keyboard_30min real callback
- ❌ `bot/keyboards/client.py:171-203` mybookings_keyboard hide-transfer-for-WorkDay (finding Gap 5)
- ❌ `bot/handlers/client.py:268-322` service_msg/confirm_cb branching (для workday)
- ❌ `tests/test_slots_command.py` (cmd_slots tests)
- ❌ Smoke-test migration 006 на prod-copy (после 005 verified на prod)

## Гейты для 5.21

- **deep-analysis-protocol:** ✅ Pass 1-4 + critic iter 2 DEEP_ENOUGH (Session 5.20 — повторно запускать НЕ нужно, та же задача). 5 minor A-E refinements учесть в impl.
- **qa-verify-and-fix:** ruff + mypy --strict bot/+tests/ + pytest (326 → ~330, +4 new tests в 5.8a).
- **qa-code-review:** рекомендуется (logic change в booking-service, high-stakes миграция). Auto-trigger: logic-change + high-stakes → запускать обязательно.
- **Pre-push:** skip (pet-project git free per AGENTS.md § git-repo-categories).
- **Commit message:** `feat(services): Booking.slot_id nullable + create_booking workday path (Этап 5.8a)`

## grep-карта для Session 5.21

### 5.8a — читать по якорям (file:line confirmed в Session 5.20):

| Якорь | Что читать | Зачем |
|---|---|---|
| `alembic/versions/004_service_price_nullable.py:36-47` | batch_alter_table pattern | Образец для migration 006 (SQLite ALTER COLUMN) |
| `alembic/versions/005_workday.py:50` | down_revision | Подтвердить "004_service_price_nullable" — 006 down_revision = "005_workday" |
| `bot/models.py:179, 205` | Booking.slot_id NOT NULL + Index ux_bookings_slot | Что менять (nullable=True, drop Index) |
| `bot/schemas.py:16-31` | BookingCreate + BookingOut | Что менять (slot_id Optional + workday_id + start_time_local) |
| `bot/services/booking.py:80-100` | BookingCreatedData + _select_open_slot | Каскад slot_id Optional в dataclass |
| `bot/services/booking.py:289-323` | `_build_start_at_from_workday(workday, start_time_local: time, business_tz)` | Helper для workday path (берёт LOCAL time, возвращает UTC datetime — finding B) |
| `bot/services/booking.py:178-268` | `_check_multi_client_capacity` + `_acquire_advisory_lock` | Race protection для workday path |
| `bot/services/booking.py:365-525` | create_booking (legacy slot path) | Образец для branching (mirror steps 3-10 в workday path) |
| `bot/services/booking.py:548-576` | CancelResult.slot_id (line 556) | Finding A — сделать Optional |
| `bot/services/booking.py:590-700` | cancel_booking (UPDATE Slot line 646) | Skip UPDATE если booking.slot_id is None |
| `bot/services/booking.py:740-960` | transfer_booking (signature line 740-743) | Add NotImplementedError guard для slot_id is None |
| `tests/test_booking.py:1602-1705` | test_create_booking_concurrent_race_integrity_error | UPDATE match string (line 1692) |
| `tests/test_multi_client.py:65-160` | _seed_capacity_test + _direct_insert_booking | Образец setup для capacity tests |
| `tests/test_openday_handlers.py:78-100` | _seed_workday helper | Образец для workday-booking setup |
| `PLANS.md:89, 181, 407` | Decision Log 006 (drift) | E — обновить PLANS.md |
| `NEXT_SESSION_PROMPT.md:4` | Prod на c64b31d, 005 не на prod | Sequencing 005→006 (post-impl, prod deploy) |

## Critic iter 2 verified facts (BP-10 A2 — для следующей сессии как grounded артефакты)

Все 7 closures iter 1 верифицированы реальными read/grep ( НЕ догадки):

1. ✅ `bot/services/admin.py:62-115` — `select(Booking).where(Booking.start_at ...)` — NO JOIN Slot. Item 7 removed из плана.
2. ✅ `tests/test_booking.py:1692` — `match="UNIQUE constraint"` — regression подтверждена (1 тест, не 2).
3. ✅ `bot/keyboards/client.py:231` — `_format_booking_summary_from_start_at` существует (5.4 infra).
4. ✅ `scheduler.py:9, 87, 143-180` — keys on `booking_id` + `start_at`, NOT slot_id. Workday-safe без изменений.
5. ✅ `alembic/versions/004_service_price_nullable.py:36-47` — `op.batch_alter_table("services")` pattern.
6. ✅ `bot/handlers/client.py:799-839` — transfer_slot_cb ловит 8 исключений (BookingNotFound, AlreadyCancelled, CancelTooLate, AlreadyTransferred, SlotAlreadyBooked, SlotInPast, SlotClosed, SlotNotAvailable), НЕТ NotImplementedError → hide-button подход (Gap 5).
7. ✅ `alembic/versions/005_workday.py:50` — `down_revision = "004_service_price_nullable"`. Migration 006 down_revision = "005_workday".

## Quick start prompt для Session 5.21

```
Продолжаем barber-bot Session 5.21 — шаг 5.8a: миграция 006 +
BookingCreate contract + create_booking workday path + cascade.

Deep-analysis Pass 1-4 + critic iter 2 уже сделаны в Session 5.20
(VERDICT: DEEP_ENOUGH). Повторно Pass 1-4 НЕ запускать — та же задача.

Сначала:
1. Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) —
   TL;DR + 5.8a plan + 5 minor A-E refinements + grep-карта.
2. Прочитай ~/PycharmProjects/barber-bot/PLANS.md (Session 5.20 в Decision Log +
   Plan of Work п.8).

== ЦЕЛЬ 5.21 ==
Шаг 5.8a: миграция 006 (Booking.slot_id nullable + drop UNIQUE + drop FK) +
BookingCreate contract (slot_id Optional + workday_id + start_time_local) +
create_booking branching (slot vs workday) + cascade в cancel/transfer
+ тесты. 6 файлов + PLANS.md update, ~438 строк.

== РИСК ==
HIGH-STAKES (миграция — one-way door, cascade в 4 функциях). Pass 1-4 + critic
iter 2 уже сделаны — VERDICT DEEP_ENOUGH. Учесть 5 minor A-E refinements в impl.

== ГЕЙТЫ ==
- deep-analysis: ✅ DEEP_ENOUGH (Session 5.20, повторно НЕ запускать).
- qa-verify-and-fix: ruff + mypy --strict bot/+tests/ + pytest (326 → ~330).
- qa-code-review: обязательно (high-stakes + logic-change).
- Pre-push: skip (pet-project git free per AGENTS.md § git-repo-categories).
- Commit message: feat(services): Booking.slot_id nullable + create_booking
  workday path (Этап 5.8a)

== ФОРМАТ ==
Pet-project, git free per AGENTS.md § git-repo-categories. Commits без
переспроса после зелёных гейтов.

== ВАЖНО ==
- 5 minor A-E refinements учесть в impl (см. выше).
- test_migration_006.py (item 6) — fallback defer to 5.8b если migration-test
  infra окажется сложным.
- 5.8b/c — НЕ в scope 5.21 (handlers/keyboards/extended-tests).
```

## Исторический контекст (Sessions 5.16-5.20 — для быстрой ориентации)

> Детальные итоги сессий 5.10 и ранее — в архиве (не нужны для 5.21).
>
> **Сводка по последним сессиям:**
>
> - **Session 5.20** (БЕЗ коммитов): deep-analysis Pass 1-4 + critic iter 1 SURFACE_LEVEL (7 gaps) + iter 2 DEEP_ENOUGH (5 minor A-E). Pass 1 critical finding: Option A (Slot материализация на лету) невозможен технически — Slot.slot_hour int 0-23 + UNIQUE(master,date,hour) ломаются на 30-мин шаге. Принят Option B (Booking.slot_id nullable + миграция). 5.8 split на 5.8a/b/c (scope ~745 строк в одной сессии невозможен).
> - **Session 5.19** (commits `94de7c9` + `2aaab58`): 5.6 occupancy check + 12 тестов. Code-reviewer LGTM, W1+S1 fixed. **326 passed + 2 skipped.**
> - **Session 5.18** (commits `48d80d6` + `163ba33`): F1 UX-fix (variant B) + 5 handler-тестов cmd_openday. 314 passed.
> - **Session 5.17** (commit `147081e`): impl 5.1 /openday + workday service. 309 passed.
> - **Session 5.16** (commit `379ebd6`): impl 5.5 multi-client + LBTM fixes. 296 passed.
> - **Session 5.15** (БЕЗ коммитов): deep-analysis Pass 1-4 + critic iter 1 NEEDS_MORE_ANALYSIS → iter 2 plan.
> - Sessions 5.10 и ранее — в архиве (blueprint.md:30 convention, не нужны для 5.21).
