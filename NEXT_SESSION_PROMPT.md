# NEXT_SESSION_PROMPT — Session 5.23: smoke-test 006 + decision 5.8b/5.9

> Дата: 2026-08-29 · **Session 5.22 завершена 2 коммитами** (`07dd1da` style + `bff88e2` feat 5.8a). 4 новых теста workday-path дописаны, гейты зелёные, code-reviewer iter 1 LBTM (F1 Critical FK name) → fix → iter 2 LGTM. Prod остаётся на `c64b31d` (Session 5.9). Миграции 005 и 006 НЕ накатывались на prod — smoke-test 006 на dev-копии обязателен (one-way door, данные Екатерины).

## ⚡ TL;DR для Session 5.23

**Цель 5.23:** (1) smoke-test миграции 006 на dev-копии prod Postgres (verify FK name `bookings_slot_id_fkey` против live DB, симметрия upgrade/downgrade) — quick task, ~30-60 мин. (2) Decision: следующая задача — 5.8b (/slots command + BookSlot30CallbackData UI workday path) ИЛИ 5.9 (/movslot / admin_move_booking). 5.8b и 5.9 — обе требуют deep-analysis Pass 1-4 + critic (HIGH-STAKES: new features, persistence layer changes).

**Главные артефакты:** этот файл + `PLANS.md` (Session 5.22 в Decision Log, line 276+; Plan of Work п.8 updated `[x] 5.8a`).

## Что УЖЕ сделано (Session 5.22 — НЕ трогать, закоммичено)

2 commits на `main`:

1. `07dd1da` `style: ruff format reflow` — collateral от ruff format run (4 файла: admin.py, slots.py, workday.py, test_start_handlers.py). Multi-line → single-line где помещается в 88 char limit.
2. `bff88e2` `feat(services): Booking.slot_id nullable + create_booking workday path (Этап 5.8a)` — 5 файлов:
   - `alembic/versions/006_booking_slot_nullable.py` (NEW, 119 строк) — upgrade: `DROP CONSTRAINT IF EXISTS bookings_slot_id_fkey` (Postgres, SQLAlchemy 2.0 default naming convention для unnamed FK) + `DROP INDEX ux_bookings_slot` + `ALTER COLUMN slot_id nullable=True` (batch_alter_table SQLite). Downgrade: `DELETE FROM bookings WHERE slot_id IS NULL` (one-way door — pet-project single-tenant) → re-add NOT NULL + UNIQUE index + FK `bookings_slot_id_fkey` (симметрично upgrade).
   - `bot/models.py:179-220` — `Booking.slot_id: Mapped[UUID | None]` (nullable=True), drop `Index("ux_bookings_slot", ...)`. FK остаётся.
   - `bot/schemas.py:1-66` — `BookingCreate` contract: `slot_id: UUID | None`, `workday_id: UUID | None`, `start_time_local: time | None`. `model_validator` XOR contract (slot_id XOR (workday_id AND start_time_local)). Half-state falls into "neither" branch.
   - `bot/services/booking.py` — branching: legacy slot path (line 429-465) + new workday path (line 466-504). B1 pattern (capture workday_id/capacity/master_id ДО `_select_or_create_client`). Cancel skip UPDATE Slot (line 742-744). Transfer NotImplementedError guard (line 919-923). Dataclasses slot_id Optional.
   - `tests/test_booking.py:2091-2321` — 4 новых теста workday-path + `_make_workday_payload` helper:
     - `test_create_booking_workday_path` (happy: slot_id=None, start_at via _build_start_at_from_workday)
     - `test_create_booking_workday_capacity_blocks_2nd` (cap=1, half-open overlap → WorkDayCapacityExceededError)
     - `test_cancel_booking_workday_no_slot_update` (skip UPDATE Slot, status='cancelled')
     - `test_transfer_booking_workday_raises_not_implemented` (NotImplementedError match="WorkDay transfer is 5.9 scope")

**Baseline тестов: 330 passed + 2 skipped** (baseline 326 → 330, +4 новых).

## Что осталось сделать в 5.23 (2 задачи)

### Задача 1 — smoke-test миграции 006 на dev-копии prod Postgres (ОБЯЗАТЕЛЬНО, ~30-60 мин)

**Зачем:** `tests/conftest.py:43` uses `Base.metadata.create_all` (НЕ alembic) → in-memory SQLite тесты НЕ покрывают миграции. FK name `bookings_slot_id_fkey` в миграции 006 verified против SQLAlchemy 2.0 default naming convention (code-reviewer iter 2 LGTM, BP-10 A2 verify), но НЕ против live prod DB. Smoke-test = `\d bookings` в psql на dev-копии prod → подтвердить что FK name = `bookings_slot_id_fkey` (а не `fk_bookings_slot_id` или иное).

**Что делать:**
1. Получить доступ к dev-копии prod Postgres (Render — отдельный DB instance для dev, НЕ prod).
2. `psql` подключение → `\d bookings` → проверить `Foreign-key constraints: "bookings_slot_id_fkey" Foreign Key (slot_id) REFERENCES slots(id)`.
3. Если имя = `bookings_slot_id_fkey` → F1 fix confirmed, миграция 006 безопасна для prod deploy. Записать в PLANS.md Session 5.23 "smoke-test passed".
4. Если имя ≠ `bookings_slot_id_fkey` (например `fk_bookings_slot_id` или `<other>`) → F1 fix НЕ валиден, нужна правка миграции 006 + новый commit + re-review.
5. (Optional) `alembic upgrade head` на dev-копии → `alembic downgrade -1` → `alembic upgrade head` (verify идемпотентность upgrade→downgrade→upgrade).
6. **Если dev-копия недоступна** → defer до момента когда будет доступна. Pet-project, single-tenant — prod deploy 006 без smoke-test = risk accept (но PLANS.md требование нарушено, пометить в Decision Log).

### Задача 2 — decision: следующая задача 5.8b ИЛИ 5.9

**5.8b** `/slots` command + `BookSlot30CallbackData` (UI workday path):
- /slots показывает 30-мин кнопки из WorkDay window (использует `get_30min_slots_from_workday` + `get_available_slots_30` из bot/services/slots.py — уже реализовано в 5.6).
- BookSlot30CallbackData → `BookingCreate(workday_id=..., start_time_local=...)` → `create_booking` workday path (5.8a готов, тесты есть).
- Hide-transfer-button для workday-only bookings (Gap 5).
- HIGH-STAKES: new feature, UI + handler + keyboard + new callback data. Deep-analysis Pass 1-4 + critic обязательны.

**5.9** `/movslot` / `admin_move_booking` (admin переносит ЛЮБОЙ booking, не owner-only):
- Отдельный сервис `admin_move_booking` без 24h rule, без client_id pin (vs `transfer_booking` который оба имеет).
- Workday-only bookings → 5.9 admin_move_booking (booking.py:919-923 guard указывает на это).
- HIGH-STAKES: new feature, persistence layer (new service), separate API. Deep-analysis Pass 1-4 + critic обязательны.

**Рекомендация:** 5.8b сначала (закрывает loop 5.8a → UI использует workday path). 5.9 — после 5.8b/c (нужен UI /mybookings с admin buttons).

### Задача 3 (optional) — `tests/test_migration_006.py` (NEW, ~60 строк)

Migration-test infra НЕ существует (`conftest.py:43` uses `Base.metadata.create_all`, НЕ alembic). Defer в 5.8b если alembic programmatic (`alembic.command.upgrade(bind, "head")`) упирается в time/context. Pet-project, single-tenant — migration-test nice-to-have, не blocker.

### Задача 4 — PLANS.md Session 5.23 запись (после smoke-test + decision)

В Decision Log добавить Session 5.23 — smoke-test результат (FK name confirmed/denied), decision 5.8b vs 5.9. Pattern из Session 5.22 (этот commit).

## Гейты для 5.23

- **deep-analysis-protocol:** НЕ нужен для smoke-test (read-only task, без code changes). Нужен для 5.8b ИЛИ 5.9 (новая фича, HIGH-STAKES).
- **qa-verify-and-fix:** НЕ нужен для smoke-test (no code changes). Нужен для 5.8b/5.9.
- **qa-code-review:** НЕ нужен для smoke-test. Нужен для 5.8b/5.9 (logic-change + new feature).
- **Pre-push:** skip (pet-project git free per AGENTS.md § git-repo-categories).
- **Commit message (если правка миграции 006 после smoke-test):** `fix(migration): 006 FK name — bookings_slot_id_fkey verified (or corrected)`.

## grep-карта для Session 5.23

| Якорь | Что | Зачем |
|---|---|---|
| `alembic/versions/006_booking_slot_nullable.py:79` | `DROP CONSTRAINT IF EXISTS bookings_slot_id_fkey` | Verify fix F1 в коде |
| `alembic/versions/006_booking_slot_nullable.py:124` | `create_foreign_key("bookings_slot_id_fkey", ...)` | Verify W1 fix в downgrade |
| `bot/services/slots.py:135-200` | `get_available_slots_30` (5.6) | Уже реализовано — 5.8b UI использует это |
| `bot/services/slots.py:get_30min_slots_from_workday` | Генератор 30-мин кнопок | 5.8b UI использует |
| `bot/services/booking.py:919-923` | `transfer_booking` NotImplementedError guard | Указывает на 5.9 scope (admin_move_booking) |
| `bot/services/booking.py:466-504` | `create_booking` workday path (5.8a) | 5.8b UI вызывает через `BookingCreate(workday_id, start_time_local)` |
| `bot/keyboards/client.py` | /book, /mybookings keyboard | 5.8b: добавить /slots button, hide-transfer для workday-only |
| `tests/test_booking.py:2091-2321` | 4 workday-path теста (5.22) | Sanity check перед 5.8b |

## Quick start prompt для Session 5.23

```
Продолжаем barber-bot Session 5.23 — smoke-test миграции 006 на dev-копии
prod Postgres + decision 5.8b vs 5.9. Session 5.22 завершилась 2 коммитами
(`07dd1da` style + `bff88e2` feat 5.8a). 5.8a impl готов, тесты прошли
(330 passed + 2 skipped), code-reviewer iter 2 LGTM.

Сначала:
1. Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) —
   TL;DR + 4 задачи + grep-карта + quick start prompt.
2. Проверь git log --oneline -3 (должно быть `bff88e2` feat 5.8a + `07dd1da`
   style + `0319836` docs handoff 5.21).
3. Прочитай PLANS.md Session 5.22 (Decision Log, line 276+) — что сделано,
   гейты, commit hash.

== ЦЕЛЬ 5.23 ==
1. Smoke-test миграции 006 на dev-копии prod Postgres:
   - psql → \d bookings → проверить FK name = `bookings_slot_id_fkey`
   - Если совпадает → F1 fix confirmed, 006 безопасен для prod deploy
   - Если НЕ совпадает → правка миграции 006 + commit + re-review
2. Decision: следующая задача — 5.8b (/slots UI) ИЛИ 5.9 (admin_move_booking).
   Рекомендация: 5.8b (закрывает loop 5.8a → UI использует workday path).

== РИСК ==
Smoke-test — read-only, без code changes, без гейтов.
5.8b/5.9 — HIGH-STAKES (new feature, persistence layer), deep-analysis
Pass 1-4 + critic обязателен.

== ГЕЙТЫ ==
- Smoke-test: deep-analysis N/A, qa-verify N/A, code-review N/A.
- 5.8b/5.9: deep-analysis Pass 1-4 + critic, qa-verify-and-fix, qa-code-review.
- Pre-push: skip (pet-project git free per AGENTS.md § git-repo-categories).

== ЧТО НЕ ТРОГАТЬ (5.22 готово, закоммичено) ==
- alembic/versions/006_booking_slot_nullable.py (если smoke-test confirmed)
- bot/models.py, bot/schemas.py, bot/services/booking.py
- tests/test_booking.py (4 workday-path теста)
```

## Исторический контекст (Sessions 5.16-5.22 — для быстрой ориентации)

> Детальные итоги сессий 5.10 и ранее — в архиве (не нужны для 5.23).
>
> **Сводка по последним сессиям:**
>
> - **Session 5.22** (commits `07dd1da` + `bff88e2`): continuation 5.21 — 4 новых теста workday-path дописаны, гейты зелёные, code-reviewer iter 1 LBTM (F1 Critical FK name mismatch `fk_bookings_slot_id` vs SQLAlchemy 2.0 default `bookings_slot_id_fkey`) → fix F1+W1 → iter 2 LGTM. **330 passed + 2 skipped.**
> - **Session 5.21** (БЕЗ commit, impl ~80% готов): 5 файлов правок готовы (миграция 006 + models + schemas + booking service + test_booking imports/docstring/match). 4 новых теста НЕ дописаны, гейты НЕ запущены. Working tree: 4 modified + 1 untracked.
> - **Session 5.20** (БЕЗ коммитов): deep-analysis Pass 1-4 + critic iter 1 SURFACE_LEVEL (7 gaps) + iter 2 DEEP_ENOUGH (5 minor A-E). Pass 1 critical finding: Option A (Slot материализация на лету) невозможен технически. Принят Option B (Booking.slot_id nullable + миграция). 5.8 split на 5.8a/b/c.
> - **Session 5.19** (commits `94de7c9` + `2aaab58`): 5.6 occupancy check + 12 тестов. Code-reviewer LGTM, W1+S1 fixed. **326 passed + 2 skipped.**
> - **Session 5.18** (commits `48d80d6` + `163ba33`): F1 UX-fix (variant B) + 5 handler-тестов cmd_openday. 314 passed.
> - **Session 5.17** (commit `147081e`): impl 5.1 /openday + workday service. 309 passed.
> - **Session 5.16** (commit `379ebd6`): impl 5.5 multi-client + LBTM fixes. 296 passed.
> - **Session 5.15** (БЕЗ коммитов): deep-analysis Pass 1-4 + critic iter 1 NEEDS_MORE_ANALYSIS → iter 2 plan.
> - Sessions 5.10 и ранее — в архиве (blueprint.md:30 convention, не нужны для 5.23).
