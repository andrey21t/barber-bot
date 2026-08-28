# NEXT_SESSION_PROMPT — Session 5.16 завершён (5.5 impl + LBTM fixes), Session 5.17 готов (5.1 /openday).

> Дата: 2026-08-28 · Session 5.16 — impl шага 5.5 multi-client + qa-verify-and-fix + qa-code-review (LBTM) + 3 правки (F1+W1+W2) + commit `379ebd6` + push. Тестов 296 passed + 2 skipped (baseline 286 → +10 в 5.5). ruff + mypy strict чисто. Prod на `c64b31d`.
> Prod остаётся на `c64b31d` (Session 5.9). Миграция 005 НЕ накатывалась на prod — smoke-test на dev-копии обязателен (one-way door, данные Екатерины).

## ⚡ TL;DR для Session 5.17 (handoff — grep-карта, НЕ копировать файлы целиком)

**Цель 5.17:** шаг **5.1 `/openday` command + FSM** (idempotent через UNIQUE master/work_date) по PLANS.md Plan of Work п.5.

**Главный артефакт:** `PLANS.md` (исключён из git в `.git/info/exclude` — читается локально). ЧИТАТЬ ПЕРВЫМ для Decision Log и Plan of Work. Далее — по grep-карте ниже (точечные чтения, НЕ целые файлы).

## Статус Этапа 5 Вариант B (на 2026-08-28):

- ✅ 5.2 WorkDay + миграция 005 (commits `90c0000` + `9345a7b`, pushed)
- ✅ 5.3 WorkDay invariants (commit `aedf1a0`, +7 → 281)
- ✅ 5.4 30-мин шаг + admin.py Booking.start_at filter (commit `069274d`, +5 → 286)
- ✅ 5.5 multi-client (commit `379ebd6`, +10 → 296, includes F1+W1+W2 LBTM fixes)
- ⏭️ **5.1 `/openday` command + FSM** — СЛЕДУЮЩИЙ (Session 5.17)
- 5.6 / 5.8 / 5.9 / 5.10 / aliases / миграция 006 — backlog

## grep-карта для Session 5.17 (читать ТОЛЬКО нужные строки, не файлы целиком):

### Решения по 5.1 (читать в PLANS.md по якорям):

| Якорь | Что читать | Зачем |
|---|---|---|
| `PLANS.md:287-295` | Gap 6 — WorkDay lifecycle one-way door | shrink refuse при активных bookings (через parens `(start_at < new OR end_at > new) AND status IN (...)`, Gap 8 SQL precedence fix) |
| `PLANS.md:311` | Race на два /openday — UNIQUE(master_id, work_date), idempotent UPDATE | Идемпотентность повторного /openday |
| `PLANS.md:264` | Подход B vs BB-007 — WorkDay на конкретную дату, не рекуррентный | Подтверждение что B не нарушает BB-007 |
| `PLANS.md:361` | Тесты 5.1 — test_openday_command, test_openday_idempotent, test_openday_shrink_with_active_bookings | Список тестов для 5.1 |
| `PLANS.md:142-186` | Session 5.16 Progress (5.5 done) | Понять что уже готово (capacity check, WorkDay model, migration 005) |

### Код для чтения (точечные grep, НЕ весь файл):

| `file:line` | Что искать | Зачем |
|---|---|---|
| `bot/models.py:134-170` | `class WorkDay`, `work_date`, `max_concurrent_clients`, `__table_args__` (UNIQUE + CheckConstraint) | Модель для /openday (CREATE/UPDATE) |
| `bot/services/slots.py:131-167` | `add_slots` DEPRECATED (Этап 5.4) | Шаблон для нового `open_workday` (НЕ реюз — slots.py Slot-логика, workday.py новый модуль) |
| `bot/services/slots.py:176-207` | `close_slot` DEPRECATED | Шаблон для `close_workday` |
| `bot/handlers/admin.py:170-244` | `cmd_addslots` handler (Command("addslots"), StateFilter(None)) | Deprecated alias → /openday, FSM старт |
| `bot/handlers/admin.py:486-518` | `admin_addslots_cb` (callback_query, StateFilter("*")) | Inline-menu callback → FSM addslots_date |
| `bot/handlers/admin.py:530-617` | `admin_addslots_calendar_cb` (SimpleCalendarCallback, FSM date) | FSM date select pattern для /openday |
| `bot/handlers/admin.py:618-704` | `admin_addslots_hours_msg` (text input → slot list) | FSM hours → TODO 5.10 inline-часы (НЕ в 5.1 scope, 5.1 = window через text ИЛИ inline) |
| `bot/services/booking.py:56-90` | `WorkDayCapacityExceededError` + `_check_multi_client_capacity` сигнатура | Образец исключения + cross-DB overlap (для `WorkDayShrinkError` + overlap query) |
| `bot/services/booking.py:234-260` | `_acquire_advisory_lock` (Postgres-only) | Образец для /openday race (опционально — UNIQUE уже даёт idempotent) |
| `tests/test_multi_client.py:51-113` | `_seed_capacity_test` helper (WorkDay + 3 slots + 3 services) | Образец setup для /openday тестов |
| `tests/test_booking_invariants.py` | 7 тестов WorkDay invariants | Образец тест-стиля (inside/outside/boundary/no-workday/inactive) |

### Что НУЖНО создать в 5.1:

| Файл | Что | Объём (оценка) |
|---|---|---|
| `bot/services/workday.py` (NEW) | `open_workday(session, master_id, work_date, start_time, end_time)`, `update_workday` (Gap 6 shrink checks), `close_workday` (set is_active=False with active-bookings check), `WorkDayShrinkError` | ~120 строк |
| `bot/handlers/admin.py` (extend) | `/openday` command + FSM openday (date → start_time → end_time → open_workday) | ~150 строк |
| `tests/test_workday_service.py` (NEW) | 5+ тестов: open, idempotent (UPDATE не INSERT), shrink refuse, expand ok, close with active bookings refuse | ~150 строк |
| `tests/test_openday_handler.py` (NEW, опционально) | FSM flow через aiogram mock | ~80 строк |

### Что НЕ трогать в 5.1:

- `bot/services/booking.py` (5.5 done, не трогать)
- `bot/services/slots.py` (DEPRECATED, замена на workday.py — НЕ реюз slots.py, там Slot-логика)
- `alembic/versions/005_workday.py` (уже applied на dev, smoke-test на prod отдельно)
- `bot/models.py` (WorkDay готов, новая миграция для CheckConstraint `max_concurrent_clients >= 1` на prod — отложить до deploy)

## Risk-класс 5.1: MEDIUM

- New service module (`workday.py`) + new exception (`WorkDayShrinkError`) + FSM
- НЕ persistence layer change (WorkDay model готова с 5.2)
- Idempotent UPDATE через UNIQUE(master_id, work_date) — стандартный pattern
- Gap 6 one-way door (shrink refuse) — единственный нетривиальный момент

**Гейты:** deep-analysis Pass 1-4 (mini, не high-stakes) → impl → qa-verify-and-fix → qa-code-review (обязательно — FSM + idempotent UPDATE + shrink checks нетривиально).

## Known limitations 5.5 (унаследованы, НЕ трогать в 5.1):

1. **cap=2 для Екатерины** — ручной `psql UPDATE work_days SET max_concurrent_clients=2 WHERE master_id=...` после deploy.
2. **Race тесты только на Postgres** — skipif SQLite.
3. **CheckConstraint `max_concurrent_clients >= 1`** — в `WorkDay.__table_args__` (models.py:163). Dev create_all picks up; prod needs separate small migration.
4. **EXCLUDE constraint drop (migration 005)** — app-level check + pg_advisory_xact_lock заменяет только для single-master serial. Cross-master parallel без DB-level гарантий (pet-project, single-tenant — Екатерина единственный master).

## Quick start prompt для Session 5.17:

```
Продолжаем barber-bot Session 5.17 — шаг 5.1 /openday command + FSM.

Сначала:
1. Прочитай ~/PycharmProjects/barber-bot/PLANS.md (living-документ, исключён из git
   через .git/info/exclude) — Progress Session 5.16 (5.5 done), Decision Log Gap 6 (line 287-295)
   + Plan of Work п.5.
2. Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) —
   grep-карта выше. ЧИТАЙ ТОЛЬКО строки из grep-карты, НЕ копируй файлы целиком
   (контекст следующей сессии дорого стоит).

== ЦЕЛЬ 5.17 ==
Шаг 5.1 /openday command + FSM:
- /openday 2026-03-17 11:00 18:00 (одно окно, text args) ИЛИ FSM (calendar → window)
- Idempotent через UNIQUE(WorkDay.master_id, WorkDay.work_date) — повторный /openday
  на ту же дату → UPDATE start_time/end_time (через update_workday с Gap 6 checks)
- WorkDay lifecycle one-way door (Gap 6, PLANS.md:287-295):
  сокращение end_time < max(active Booking.end_at) → refuse с WorkDayShrinkError
  (список conflict bookings в сообщении)
- Реализация в НОВОМ модуле bot/services/workday.py (НЕ slots.py — там Slot-логика)

== ПЛАН IMPL ==
1. bot/services/workday.py (NEW) — open_workday, update_workday (Gap 6 checks),
   close_workday, WorkDayShrinkError. ~120 строк.
2. bot/handlers/admin.py (extend) — /openday command + FSM openday (date → start_time →
   end_time → open_workday). ~150 строк.
3. tests/test_workday_service.py (NEW) — 5+ тестов: open, idempotent, shrink refuse,
   expand ok, close with active bookings. ~150 строк.

== РИСК ==
MEDIUM. New service module + FSM + idempotent UPDATE + shrink checks (Gap 6 one-way door).
НЕ persistence layer change (WorkDay готова с 5.2).
deep-analysis Pass 1-4 (mini), опционально critic если Pass 1 классифицирует как high-stakes.

== ГЕЙТЫ ==
- deep-analysis-protocol: Pass 1-4 (mini — не high-stakes по self-verify)
- qa-verify-and-fix: ruff + mypy --strict + pytest (296 → ~301-306)
- qa-code-review: ОБЯЗАТЕЛЬНО (FSM + idempotent UPDATE + shrink checks — нетривиально)
- Pre-push НЕ нужен — barber-bot личный репо (AGENTS.md § git-repo-categories)

== ФОРМАТ ==
MY-VIBE-RULES.md — dev-режим, гейты: deep-analysis → impl → verify → code-review.
Pet-project, git free (AGENTS.md § git-repo-categories).

== ВАЖНО ==
НЕ копируй файлы целиком в промпт следующей сессии. Используй grep-карту выше —
читай только нужные строки. PLANS.md — ЧИТАТЬ ПЕРВЫМ (живёт локально, в git
исключён через .git/info/exclude).
```

## Исторический контекст (Session 5.16 и ранее — читать ТОЛЬКО при необходимости)

> Детальные итоги сессий 5.15, 5.14, 5.13, 5.11, 5.9, 5.8, 5.7, 5.6, Sessions 1-2 —
> в архиве ниже. Для 5.17 они НЕ нужны — grep-карта + PLANS.md Decision Log
> содержат все нужные ссылки. Читай архив только если нужны детали impl предыдущих фаз
> (например, шаблон FSM из 5.3 или образец миграции из 5.2).
>
> **Сводка по сессиям (для быстрой ориентации, без деталей):**
>
> - **Session 5.16** (commit `379ebd6`, pushed): impl 5.5 multi-client + LBTM fixes (F1+W1+W2). 296 passed + 2 skip. См. PLANS.md:142-186.
> - **Session 5.15** (БЕЗ коммитов, analysis only): deep-analysis Pass 1-4 + critic iter 1 NEEDS_MORE_ANALYSIS → iter 2 plan. См. PLANS.md:133-141.
> - **Session 5.14** (commit `069274d`, pushed): 5.4 — 30-мин шаг + `_build_start_at_from_workday` + admin.py Booking.start_at filter. 286 passed. См. PLANS.md:90-131 + архив ниже (offset 430).
> - **Session 5.13** (commit `aedf1a0`, pushed): 5.3 — WorkDay invariants в create_booking. 281 passed. См. PLANS.md:56-89 + архив (offset 526).
> - **Session 5.12** (commits `90c0000` + `9345a7b`, pushed): 5.2 — WorkDay модель + миграция 005 (DROP EXCLUDE, INSERT из Slot). 274 passed. См. PLANS.md:38-55.
> - **Session 5.11** (БЕЗ коммитов, analysis only): deep-analysis-critic iter 1 → NEEDS_MORE_ANALYSIS → iter 2 DEEP_ENOUGH. PLANS.md создан. См. PLANS.md:25-37 + архив (offset 636).
> - **Sessions 1-2**: cross-DB schema + миграции 001/002 + Render deploy. См. архив (offset 655+).

---
