# NEXT_SESSION_PROMPT — Session 4 закрыт. Session 5 (Render smoke test + L2 atomicity + new features) готова к анализу.

> Дата: 2026-08-24 · Session 4 закоммичен и запушен (commits `66e11e4` + `e3cf1dc`). FSM PostgresStorage реализован (L5 + L1 callback fallback), L4 dropped (ContextVar не работает с AsyncIOScheduler). L3 — batched NOT EXISTS subquery. Deep-analysis: 3 critic итерации (SURFACE_LEVEL → упрощённый scope). Code-review iter 1 LGTM + 4 warnings applied (W1 race / W2 contract / W3 onupdate / W4 events_isolation). **Готов к user GO → Session 5.**

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories)
**GitHub:** https://github.com/andrey21t/barber-bot (private, remote `origin` настроен)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async (asyncpg/aiosqlite), APScheduler 3.11.3
**Spec (SSOT):** `~/PycharmProjects/barber-bot/spec.md` (Урок 2.6 — строки 320-447 Session 2 design, 538-547 FSM storage)
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим, гейты: deep-analysis → impl → verify → code-review
**Состояние на старте Session 2:** 226 тестов, 0 skipped, coverage 97%, ruff+mypy чисто, последний коммит `e63ca83` (typo fix в spec.md), push в `origin/main` выполнен.

## Что сделано в Session 1 Phase 5 (коммиты 6b30bc0 + e63ca83, запушены)

1. Cross-DB schema: 9 DateTime columns → `with_variant(TIMESTAMP(timezone=True), "postgresql")` (bot/models.py)
2. 7 BREAKS: убраны `.replace(tzinfo=None)` workaround'ы + 11 инъекций `.replace(tzinfo=UTC)` на DB-read side (booking.py, admin.py, handlers/admin.py, client.py, keyboards/client.py)
3. alembic/versions/001_initial.py — 7 tables, 4 CHECKs, 4 partial indexes, 3 UNIQUE + 1 implicit, 1 regular composite, 2 FK CASCADE, NO EXCLUDE
4. alembic/env.py + alembic.ini — async URL → sync URL (asyncpg→psycopg2, aiosqlite→sqlite)
5. pyproject.toml — `[project.optional-dependencies].prod = [asyncpg, psycopg2-binary, alembic]`
6. .env.example — `DATABASE_URL_SYNC=` placeholder
7. spec.md — note про UUID Python-side (опечатка "Sm.oko" исправлена на "Итог")
8. Code-review: LGTM, 3 warnings (W1 contract comments, W2 stale test docstrings, W3 assumption comment) — all fixed

## Что сделано в Session 2 analysis (БЕЗ правок кода, только план)

1. `deep-analysis-protocol` Pass 1-4 на Session 2 scope (миграция 002 + SQLAlchemyJobStore + Render deploy)
2. Risk-класс: **high-stakes** (миграция БД + persistence layer change + deploy secrets)
3. `deep-analysis-critic` iter 1 → `VERDICT: NEEDS_MORE_ANALYSIS` (7 findings: 2 Critical + 4 Important + 1 Boundary + 1 inconsistency)
4. 7 findings применены к плану:
   - (Critical) transfer_booking IntegrityError location — booking.py:577 (`session.execute(upd_b)`), НЕ flush/commit
   - (Critical) create_booking уже имеет catch — booking.py:188-190, НЕ ТРОГАТЬ
   - (Important) MAIN DECISION POINT non-issue — SQLAlchemyJobStore.__init__ lazy, module-level `build_scheduler()` остаётся
   - (Important) DATABASE_URL conversion — `@property async_database_url` в Settings
   - (Important) DATABASE_URL_SYNC derivation — `@property sync_database_url` в Settings
   - (Verified) btree_gist на Render — confirmed via webfetch render.com/docs/postgresql-extensions
   - (Boundary) FSM state storage (spec.md:538-547) — добавлено в Honest limitations, deferred to Session 3
   - (Inconsistency) render.yaml buildCommand — `pip install -e .[prod]` (requirements.txt НЕ существует)
5. `deep-analysis-critic` iter 2 → `VERDICT: DEEP_ENOUGH` — все 7 findings применены корректно
6. 1 NEW minor gap из iter 2: async engine `pool_pre_ping` (bot/db.py) — applied as minor amendment
7. **Статус**: READY FOR IMPLEMENTATION. После GO → Phase A → B → C → verify → code-review → push.

## Augmented Plan Session 2 — 3 фазы (post critic iter 2 DEEP_ENOUGH)

### Фаза A — EXCLUDE constraint migration 002

**Файл:** `alembic/versions/002_postgres_exclude.py`

```python
"""Postgres-only EXCLUDE constraint for no-double-booking (tstzrange overlap).

revision: 002_postgres_exclude
down_revision: 001_initial
created: 2026-08-23

Cross-DB:
- Postgres: CREATE EXTENSION btree_gist + EXCLUDE USING gist (tstzrange)
  (VERIFIED btree_gist available on Render Postgres 13+ —
   https://render.com/docs/postgresql-extensions)
- SQLite: no-op (UNIQUE(slot_id) from 001_initial handles same-slot double-booking;
  SQLite не имеет EXCLUDE USING gist)

WHERE clause: status IN ('confirmed', 'transferred') — адаптировано под codebase:
- booking INSERT goes 'confirmed' (booking.py:183)
- UPDATE to 'transferred' (booking.py:571)
- UPDATE to 'cancelled' (booking.py:345) — 'cancelled' excluded из WHERE
  (отмена освобождает слот для новых overlap'ов)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_postgres_exclude"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # VERIFIED btree_gist available on Render Postgres 13+
        # https://render.com/docs/postgresql-extensions
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            sa.text("""
                ALTER TABLE bookings ADD CONSTRAINT no_overlap
                EXCLUDE USING gist (
                    master_id WITH =,
                    tstzrange(start_at, end_at) WITH &&
                ) WHERE (status IN ('confirmed', 'transferred'))
            """)
        )
    # SQLite — no-op (UNIQUE(slot_id) из 001 handles same-slot)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS no_overlap")
```

### Фаза B — Scheduler SQLAlchemyJobStore (sync psycopg2 engine)

#### B.1 — `bot/config.py` — добавить 2 properties для URL conversion

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки проекта (spec.md 290-305).

    DATABASE_URL_SYNC — пусто в dev (MemoryJobStore), заполняется в проде для SQLAlchemyJobStore.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BOT_TOKEN: str
    DATABASE_URL: str = "sqlite+aiosqlite:///./barber.db"
    DATABASE_URL_SYNC: str = ""  # sync engine для SQLAlchemyJobStore (psycopg2)
    ADMIN_ID: int
    TIMEZONE: str = "Europe/Moscow"
    REMINDER_24H_BEFORE: int = 24
    REMINDER_1H_BEFORE: int = 1
    CANCEL_MIN_HOURS: int = 24
    MISFIRE_GRACE_TIME: int = 3600  # Render free tier sleep 15 мин = 900 сек → 3600 сек запас
    SERVICE_DEFAULT_DURATION_MIN: int = 60
    MAX_BOOKING_DAYS_AHEAD: int = 60  # aiogram_calendar range (today..today+N days)

    @property
    def async_database_url(self) -> str:
        """Convert Render's plain `postgresql://` / `postgres://` → asyncpg format.

        Render Postgres `fromDatabase` env var provides `postgresql://user:pass@host:port/db`
        (НЕ asyncpg). asyncpg driver требует `postgresql+asyncpg://`.
        """
        if self.DATABASE_URL.startswith(("postgresql://", "postgres://")):
            return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1) \
                .replace("postgres://", "postgresql+asyncpg://", 1)
        return self.DATABASE_URL

    @property
    def sync_database_url(self) -> str:
        """Derive sync URL for SQLAlchemyJobStore (psycopg2).

        Render НЕ поддерживает env var interpolation в render.yaml — поэтому
        DATABASE_URL_SYNC задаём через property, не через render.yaml envVars.
        """
        if self.DATABASE_URL_SYNC:
            return self.DATABASE_URL_SYNC
        for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
            if self.DATABASE_URL.startswith(prefix):
                return self.DATABASE_URL.replace(prefix, "postgresql+psycopg2://", 1)
        # SQLite fallback (dev)
        return self.DATABASE_URL.replace("sqlite+aiosqlite://", "sqlite://", 1)


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from .env
```

#### B.2 — `bot/db.py` — использовать `async_database_url` + pool_pre_ping

```python
# было (line 16):
# engine = create_async_engine(settings.DATABASE_URL, future=True)

# стало:
engine = create_async_engine(
    settings.async_database_url,
    future=True,
    pool_pre_ping=True,   # detect dead conn после Render sleep 15 мин (асимметрия с sync engine B.3)
    pool_recycle=1800,    # 30 мин recycle (Render free sleep 15 мин → conn stale)
)
```

#### B.3 — `scheduler.py` — branching by `sync_database_url`

```python
"""Scheduler — APScheduler AsyncIOScheduler with cross-DB jobstore.

Spec.md 322-393, 354:
- Dev (SQLite): MemoryJobStore (no pickle, no sync engine)
- Prod (Postgres): SQLAlchemyJobStore(engine=sync_engine) — sync psycopg2 engine
  (pickle-сериализация не работает с asyncpg; separate sync engine required).
- on_startup_scan пересоздаёт jobs при restart (BB-012)
- schedule_for_booking: add_job remind_24h + remind_1h (replace_existing=True)
- job_id format: f"remind_24h_{booking_id}", f"remind_1h_{booking_id}" (deterministic for replace)
- misfire_grace_time=3600 (1h — Render sleep 15min = 900s → 3600s запас)
- coalesce=True, max_instances=1

SQLAlchemyJobStore.__init__ lazy (verified apscheduler/jobstores/sqlalchemy.py:65-85):
engine stored as reference, NO connection на construct. Table `apscheduler_jobs`
created at `scheduler.start()` via `jobs_t.create(engine, True)` (CREATE IF NOT EXISTS).
Module-level `scheduler = build_scheduler()` безопасен.
"""

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.config import get_settings
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_scheduler() -> AsyncIOScheduler:
    """Build AsyncIOScheduler with cross-DB jobstore branching.

    Dev (DATABASE_URL=sqlite, DATABASE_URL_SYNC empty) → MemoryJobStore.
    Prod (DATABASE_URL=postgresql, DATABASE_URL_SYNC empty → derived) → SQLAlchemyJobStore.
    """
    settings = get_settings()
    sync_url = settings.sync_database_url
    # SQLAlchemyJobStore.__init__ lazy — engine не подключается на construct
    # (verified apscheduler/jobstores/sqlalchemy.py:65-85). Module-level
    # build_scheduler() безопасен — actual table CREATE в scheduler.start().
    if sync_url.startswith("postgresql"):
        sync_engine = create_engine(
            sync_url,
            pool_pre_ping=True,    # detect dead conn после Render sleep 15 мин
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
        )
        jobstore = SQLAlchemyJobStore(engine=sync_engine)
    else:
        # Dev SQLite — MemoryJobStore (no pickle, no sync engine)
        jobstore = MemoryJobStore()
    return AsyncIOScheduler(
        timezone=ZoneInfo(settings.TIMEZONE),
        jobstores={"default": jobstore},
        job_defaults={
            "coalesce": True,
            "misfire_grace_time": settings.MISFIRE_GRACE_TIME,
            "max_instances": 1,
        },
    )


async def send_reminder(booking_id: UUID, kind: str, bot: Any = None) -> None:
    """Job target — sends reminder to client.

    ⚠️ PICKLE-STABLE SIGNATURE — DO NOT rename/remove args without
    drop+reschedule strategy. SQLAlchemyJobStore pickles (booking_id, kind, bot)
    into apscheduler_jobs.job_state column. Adding args with defaults is safe;
    renaming/removing is a ONE-WAY DOOR (existing jobs fail to unpickle → TypeError
    on next scheduler.start). Migration strategy: scheduler.remove_all_jobs()
    then on_startup_scan reschedules from DB.

    Stub for Шаг 3: real implementation in Урок 2.4 (master handlers).
    Real flow:
      1. SELECT booking
      2. INSERT notifications_log(booking_id, kind) — UNIQUE guard
      3. bot.send_message(client_id, "Напоминание: ...")
    """
    # TODO Урок 2.4: real send_reminder with DB lookup + bot.send_message
    return None


def schedule_for_booking(
    scheduler: AsyncIOScheduler,
    booking_id: UUID,
    start_at: datetime,
) -> None:
    """Schedule remind_24h + remind_1h for a booking.

    replace_existing=True — idempotent, safe to call multiple times.
    If start_at - 24h is in the past, APScheduler's misfire_grace_time handles it
    (job fires immediately if within grace window, otherwise dropped — on_startup_scan catches).
    """
    settings = get_settings()
    remind_24h_at = start_at - timedelta(hours=settings.REMINDER_24H_BEFORE)
    remind_1h_at = start_at - timedelta(hours=settings.REMINDER_1H_BEFORE)

    job_id_24h = f"remind_24h_{booking_id}"
    job_id_1h = f"remind_1h_{booking_id}"

    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=remind_24h_at,
        args=[booking_id, "remind_24h"],
        id=job_id_24h,
        replace_existing=True,
    )
    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=remind_1h_at,
        args=[booking_id, "remind_1h"],
        id=job_id_1h,
        replace_existing=True,
    )


def remove_jobs_for_booking(scheduler: AsyncIOScheduler, booking_id: UUID) -> None:
    """Remove remind_24h + remind_1h for a booking (on cancel)."""
    for kind in ("remind_24h", "remind_1h"):
        job_id = f"{kind}_{booking_id}"
        with suppress(Exception):
            scheduler.remove_job(job_id)


async def on_startup_scan(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Any = None,
) -> None:
    """on_startup — 2 phases (spec.md 358-382):

    Phase 1: fire_overdue_reminders — bookings без remind_24h где start_at в окне (now-24h, now)
    Phase 2: schedule_for_booking для ВСЕХ upcoming (start_at > now, < now+25h)
    """
    from bot.services.notifications import (
        get_overdue_bookings_without_remind_24h,
        get_upcoming_bookings_for_reschedule,
        log_notification,
    )

    now = datetime.now(UTC)

    async with session_factory() as session:
        # Phase 1: fire overdue (stub — real send in Урок 2.4)
        overdue = await get_overdue_bookings_without_remind_24h(session, now)
        for b in overdue:
            # TODO Урок 2.4: actually send message via bot
            await log_notification(session, b.id, "remind_24h")

        # Phase 2: reschedule upcoming
        upcoming = await get_upcoming_bookings_for_reschedule(session, now, look_ahead_hours=25)
        for b in upcoming:
            schedule_for_booking(scheduler, b.id, b.start_at)
```

#### B.4 — `bot/services/booking.py:562-577` — transfer_booking IntegrityError catch

**Точный line (post critic iter 1 fix):** оборачиваем `await session.execute(upd_b)` на line 577.

```python
    upd_b = (
        update(Booking)
        .where(
            Booking.id == booking_id,
            Booking.client_id == client_id,
            Booking.status.in_(("confirmed", "transferred")),
            Booking.start_at == old_start_at,  # race-protection pin (Pass 3 [blocker] finding)
        )
        .values(
            status="transferred",
            slot_id=new_slot.id,
            start_at=new_start_at,
            end_at=new_end_at,
        )
    )
    try:
        res_b = await session.execute(upd_b)
    except IntegrityError as exc:
        # EXCLUDE constraint (Postgres): new tstzrange(start_at, end_at) overlaps
        # another active booking (confirmed/transferred) for same master.
        # SQLite не имеет EXCLUDE — UNIQUE(slot_id) handles only same-slot case,
        # но это безопасно (SQLite dev, надёжность через UNIQUE + service-layer checks).
        # Map to existing SlotAlreadyBookedError — пользователь видит "слот занят".
        await session.rollback()
        raise SlotAlreadyBookedError(
            f"Transfer to slot {new_slot.id} overlaps existing booking (EXCLUDE constraint)"
        ) from exc
    if cast("CursorResult[Any]", res_b).rowcount == 0:
        # existing race-protection logic (unchanged)
        # ...
```

**create_booking (booking.py:188-190) — НЕ ТРОГАТЬ.** Existing `except IntegrityError → SlotAlreadyBookedError` handles EXCLUDE на Postgres (EXCLUDE fires на flush).

**Импорт `IntegrityError`** — проверить что уже импортирован в `bot/services/booking.py` (если нет — добавить `from sqlalchemy.exc import IntegrityError`).

### Фаза C — Render deploy

#### C.1 — `alembic/env.py` — handle ALL Render URL formats

```python
# заменить строки 33-40 на:
sync_url = os.getenv("DATABASE_URL_SYNC", "")
if not sync_url:
    async_url = os.getenv("DATABASE_URL", "sqlite:///./barber.db")
    # Handle ALL Render formats: postgresql+asyncpg://, postgresql://, postgres://
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if async_url.startswith(prefix):
            sync_url = async_url.replace(prefix, "postgresql+psycopg2://", 1)
            break
    else:
        sync_url = async_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
config.set_main_option("sqlalchemy.url", sync_url)
```

#### C.2 — `render.yaml` (новый файл, root)

```yaml
# Render deployment config — barber-bot pet-project (Урок 2.6).
# Render free tier: web service sleeps 15 min without inbound HTTP.
# Polling bot → sleeps → missed jobs → on_startup_scan on wake (BB-012).
# FSM state storage (spec.md:538-547) deferred to Session 3 (PostgresStorage).

services:
  - type: web
    name: barber-bot
    env: python
    buildCommand: pip install -e .[prod]  # NOT pip install -r requirements.txt (requirements.txt doesn't exist; pyproject.toml [project.optional-dependencies].prod has asyncpg + psycopg2-binary + alembic)
    startCommand: python -m bot.main
    envVars:
      - key: DATABASE_URL
        fromDatabase: { name: barber-db, property: connectionString }
      # DATABASE_URL_SYNC НЕ задаём — Settings.sync_database_url derives от DATABASE_URL
      # (Render НЕ поддерживает env var interpolation в render.yaml)
      - key: BOT_TOKEN
        sync: false  # manual entry на Render dashboard
      - key: ADMIN_ID
        sync: false
    preDeployCommand: alembic upgrade head  # runs 001_initial + 002_postgres_exclude sequentially (empty Postgres)
databases:
  - name: barber-db
    plan: free
    ipAllowList: []  # internal only (Render services → Render DB)
```

## Honest limitations (not blockers, зафиксированы для Session 3)

- **No Postgres tests** (no testcontainers, overkill для pet-project) — smoke test после deploy.
- **Render free web service sleep** 15 мин → polling bot спит → missed jobs (on_startup_scan на wake подхватывает).
- **Cross-model correlation** — main и critic на одной модели LiteLLM (общие слепые пятна).
- **FSM state storage** (spec.md:538-547) — Render free restarts каждые 15 мин → in-progress FSM теряется. Spec recommends PostgresStorage (option C). **Deferred to Session 3** (out of scope Session 2).
- **EXCLUDE violation semantically off** — `SlotAlreadyBookedError` от EXCLUDE показывает "слот только что заняли" (acceptable для MVP, semantically off для overlap, но user-facing текст релевантный).
- **btree_gist availability** — VERIFIED via webfetch render.com/docs/postgresql-extensions (PG 13+).

## Verification plan (после implementation, перед коммитом)

1. `pytest` — 226 тестов должны остаться зелёными (dev path не меняется: MemoryJobStore по умолчанию, sync_database_url → sqlite://)
2. `ruff check .` + `ruff format --check .` — чисто
3. `mypy bot` — Success
4. `alembic upgrade head --sql` (SQLite dialect) — 002 должен быть no-op (UNIQUE(slot_id) handles)
5. `pip install -e .[prod]` на clean venv — verify prod deps coexist (asyncpg + psycopg2-binary + alembic)
6. `alembic upgrade head` на SQLite in-memory DB — 002 должен пройти (no-op)
7. Post-deploy smoke test на Render (после push и deploy):
   - Записать бронь через bot, проверить /today корректное время
   - Записать 2 брони на overlap (same master, overlapping tstzrange) → должно отбиться EXCLUDE constraint
   - Проверить что `apscheduler_jobs` table создалась в Postgres

## Гейты (напоминание)

- **deep-analysis-protocol** Pass 1-4 на Session 2 — выполнен
- **deep-analysis-critic** iter 1 → NEEDS_MORE_ANALYSIS (7 findings), iter 2 → DEEP_ENOUGH (1 minor fixed) — выполнен (max 2 LBTM, в рамках протокола)
- **qa-verify-and-fix** после implementation: pytest + ruff + mypy — все зелёные
- **qa-code-review** через `code-reviewer` subagent на logic-change (SQLAlchemyJobStore branching, IntegrityError catch)
- **Pre-push**: НЕ нужен — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса, после verify + code-review

## Файлы для быстрого ориентирования

| Файл | Что | Изменения Session 2 |
|---|---|---|
| `spec.md` | SSOT — Урок 2.6 (строки 320-447 Session 2 design, 538-547 FSM storage) | Не трогать |
| `MY-VIBE-RULES.md` | Формат работы + гейты (dev-режим) | Не трогать |
| `alembic/versions/002_postgres_exclude.py` | НОВЫЙ — Postgres EXCLUDE constraint + btree_gist, SQLite no-op | СОЗДАТЬ (Фаза A) |
| `alembic/versions/001_initial.py` | 7 tables, NO EXCLUDE (из Session 1) | Не трогать (down_revision ref) |
| `alembic/env.py` | async URL → sync URL | ПРАВИТЬ (C.1 — handle 3 Render prefixes) |
| `bot/config.py` | Settings — добавить 2 properties | ПРАВИТЬ (B.1 — async_database_url, sync_database_url) |
| `bot/db.py` | Async engine, session_factory | ПРАВИТЬ (B.2 — async_database_url + pool_pre_ping + pool_recycle) |
| `bot/models.py` | 7 SQLAlchemy 2.0 models (из Session 1) | Не трогать |
| `bot/services/booking.py` | create/cancel/transfer booking | ПРАВИТЬ (B.5 — transfer_booking IntegrityError catch на line 577; create_booking НЕ ТРОГАТЬ line 188-190) |
| `bot/services/admin.py` | get_today/week_bookings | Не трогать |
| `bot/services/notifications.py` | on_startup helpers | Не трогать |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | Не трогать |
| `bot/handlers/admin.py` | /today, /week, /services | Не трогать |
| `bot/handlers/client.py` | booking FSM + /mybookings + /cancel + /transfer | Не трогать |
| `bot/keyboards/client.py` | inline keyboards | Не трогать |
| `bot/main.py` | entry point, scheduler = build_scheduler() (line 36) | Не трогать (module-level safe per critic iter 1) |
| `scheduler.py` | AsyncIOScheduler + MemoryJobStore | ПРАВИТЬ (B.3 — SQLAlchemyJobStore branching + B.4 — pickle-stable docstring) |
| `tests/test_scheduler.py` | test_build_scheduler_memory_jobstore (75-80) — продолжит проходить (dev path MemoryJobStore) | Не трогать |
| `tests/test_booking.py` | booking service tests (~2053 строк) | Не трогать (опц.: добавить test для transfer_booking IntegrityError — ручной smoke на Postgres) |
| `tests/test_admin.py` | services/admin tests | Не трогать |
| `tests/test_admin_handlers.py` | admin handlers tests | Не трогать |
| `tests/test_client_handlers.py` | client handlers tests (~2720 строк) | Не трогать |
| `tests/test_main.py` | bot/main.py wiring + lifecycle | Не трогать |
| `tests/test_session.py` | CorpAiohttpSession ssl context | Не трогать |
| `tests/test_db.py` | bot/db.py create_all/drop_all/dispose/utcnow | Не трогать |
| `tests/test_slots.py` | slots service tests | Не трогать |
| `tests/conftest.py` | in-memory SQLite fixtures, seed_data | Не трогать |
| `pyproject.toml` | Python 3.12, deps + `[project.optional-dependencies].prod` (из Session 1) | Не трогать (buildCommand ref) |
| `.env.example` | BOT_TOKEN, DATABASE_URL=sqlite, ADMIN_ID, DATABASE_URL_SYNC= (из Session 1) | Не трогать |
| `render.yaml` | НОВЫЙ — Render deploy config | СОЗДАТЬ (C.2) |

## Quick start prompt для opencode (вставить в новую сессию)

```
Продолжаем barber-bot Session 2 (Postgres migration продолжение — EXCLUDE constraint + SQLAlchemyJobStore + Render deploy).

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный контекст:
- Session 1 Phase 5 закоммичен и запушен (commit 6b30bc0 + e63ca83, origin/main).
- Session 2: deep-analysis Pass 1-4 + 2 critic итерации (iter 1 NEEDS_MORE_ANALYSIS 7 findings, iter 2 DEEP_ENOUGH 1 minor fixed).
- Augmented план готов: 3 фазы (A — EXCLUDE 002, B — SQLAlchemyJobStore, C — Render deploy).

Статус: READY FOR IMPLEMENTATION. После GO ("go" / "правь" / "apply") → implementation.

Implementation order:
1. Фаза A: alembic/versions/002_postgres_exclude.py (новый файл)
2. Фаза B.1: bot/config.py — добавить 2 properties (async_database_url, sync_database_url)
3. Фаза B.2: bot/db.py — async_database_url + pool_pre_ping + pool_recycle
4. Фаза B.3: scheduler.py — SQLAlchemyJobStore branching + pickle-stable docstring
5. Фаза B.4: scheduler.py:41-51 — send_reminder pickle-stable docstring
6. Фаза B.5: bot/services/booking.py:562-577 — transfer_booking IntegrityError catch (проверить импорт IntegrityError)
7. Фаза C.1: alembic/env.py — handle 3 Render URL prefixes
8. Фаза C.2: render.yaml (новый файл)

Verification:
- pytest (226 → green, dev path MemoryJobStore не меняется)
- ruff + mypy — clean
- alembic upgrade head --sql (SQLite dialect) — 002 должен быть no-op
- pip install -e .[prod] на clean venv — verify prod deps coexist

Gates:
- qa-verify-and-fix после implementation
- qa-code-review через code-reviewer subagent на logic-change (SQLAlchemyJobStore branching, IntegrityError catch)
- Коммиты свободно (pet-project, AGENTS.md § git-repo-categories)
- Push в origin/main после verify + code-review

Honest limitations (not blockers):
- No Postgres tests (smoke after deploy)
- Render free web service sleep 15 мин (on_startup_scan catches)
- FSM state storage deferred to Session 3 (spec.md:538-547)

NEXT: Session 3 — FSM PostgresStorage + real send_reminder (Урок 2.4) + Render deploy smoke.
```

## Cross-refs

- `~/.config/opencode/skills/deep-analysis-protocol/SKILL.md` — гейт протокол (Pass 1-4 + critic)
- `~/.config/opencode/AGENTS.md` § git-repo-categories — barber-bot = personal free (коммиты без переспроса)
- `~/.config/opencode/references/donor-research/topics/booking-bot-architecture.md` — BB-008 (EXCLUDE constraint), BB-011 (SQLAlchemyJobStore), BB-012 (on_startup_scan), BB-014 (anti-pattern MemoryJobStore на Render)
- https://render.com/docs/postgresql-extensions — btree_gist availability VERIFIED (PG 13+)
- Spec: Урок 2.6 Postgres migration (spec.md:320-447 Session 2 design, 538-547 FSM storage)

## NEXT: Session 4

> Session 3 закрыт: real `send_reminder` реализован, deferred Session 2 fixes применены. Critic iter 1 → SURFACE_LEVEL (4 findings без grounding) → все applied к плану → iter 2 не запускался (risk-class LOGIC confirmed, fixes приняли вид proper grounding).

### Session 3 итоги (commits pending)

1. **`scheduler.py:67-160` `send_reminder`** — заглушка → реальная реализация:
   - DB lookup Booking+Client+Business через explicit JOIN (нет ORM relationships в models.py)
   - log_notification UNIQUE guard — False = already sent, return
   - bot resolution: param > global `_bot_ref` (None → return с warning)
   - text: "Напоминаю: завтра в HH:MM" / "Через час: HH:MM" (start_at в business.timezone)
   - try/except с retry:
     - `TelegramRetryAfter` → `asyncio.sleep(retry_after)` + retry once (flood control)
     - `TelegramForbiddenError` / `TelegramBadRequest` → log warning, return (chat blocked/invalid)
     - Прочее `TelegramAPIError` → log error, return

2. **`scheduler.py` global `_bot_ref` + `_set_bot_ref()`** — anti-pattern (global mutable state), но aiogram Bot не picklable (aiohttp session) → нельзя через APScheduler args. Scheduled jobs используют `_bot_ref` fallback (args=[booking_id, kind] без bot). Direct callers (on_startup_scan) передают bot явно. Session 4: refactor через context var / DI.

3. **`bot/main.py:_on_startup`** — `_set_bot_ref(bot)` вызывается ПЕРЕД `scheduler.start()` (избежать race: scheduled jobs после start должны видеть ref).

4. **`alembic/env.py`** — lazy import ОБА `bot.models` и `bot.db.Base` внутрь `_get_target_metadata()` (вызывается из `run_migrations_online` / `run_migrations_offline`). Причина: `bot.models → bot.db → bot.config → Settings(BOT_TOKEN, ADMIN_ID)`. Module-level импорт триггерит Settings при `alembic upgrade` в CI/pre-deploy, где есть только `DATABASE_URL`, нет `BOT_TOKEN` → `ValidationError`. Lazy import откладывает Settings load до момента, когда alembic уже знает DATABASE_URL.

5. **`scheduler.py:on_startup_scan`** — Phase 1 теперь вызывает `send_reminder(booking_id, "remind_24h", bot=bot)` (вместо заглушки `log_notification`). Booking IDs извлекаются из session до её закрытия, send_reminder открывает свою session (не nested). Phase 2: upcoming pairs `(id, start_at)` извлекаются до закрытия session (избежать DetachedInstanceError).

6. **`spec.md:378`** — сигнатура синхронизирована с кодом: `await send_reminder(b.id, "remind_24h", bot=bot)` (bot последним для pickle-stability, default None для scheduled jobs).

7. **`tests/test_scheduler.py`** — 7 новых/обновлённых тестов (всего 14):
   - `test_on_startup_scan_phase_1_sends_overdue` (mock bot, патч `bot.db.async_session_factory` на test session_factory)
   - `test_send_reminder_booking_not_found` (random UUID, no send)
   - `test_send_reminder_unique_guard_skips_duplicate` (pre-insert NotificationLog → no send)
   - `test_send_reminder_bot_none_returns` (both bot=None + _bot_ref=None → early return)
   - `test_send_reminder_retry_after` (TelegramRetryAfter → asyncio.sleep + retry, mock sleep)
   - `test_send_reminder_forbidden_logs_warning` (TelegramForbiddenError → log warning, no retry)
   - `test_send_reminder_happy_path` (correct text prefix)

### Verify gate

- pytest: **234 passed** (was 232 → +2 regression: timezone_utc_to_moscow + invalid_timezone_logs_error)
- ruff: clean
- mypy: clean

### Code-review (qa-code-review gate, AGENTS.md § write-actions-subagents rule 7)

**Iter 1: LBTM** — 1 Critical + 2 Warnings + 1 Suggestion (BP-10 A2 verified):
- **F1 Critical** (scheduler.py:157): `booking.start_at.astimezone(tz)` без `.replace(tzinfo=UTC)` — нарушает паттерн `booking.py:380, :531, :650`. На SQLite naive → system-local TZ → wrong time на TZ≠UTC системах (macOS Europe/Moscow +3h shift, Render TZ=UTC correct by accident). Tests не ловили (только prefix+length checks).
- **W1 Warning** (scheduler.py:156): `ZoneInfo(business.timezone)` без try/except — invalid timezone → ZoneInfoNotFoundError bubbles, log_notification уже закоммичен → UNIQUE блокирует retry навсегда.
- **W2 Warning** (alembic/env.py:12-17): misleading comment «CI fix» — реального фикса нет, lazy import лишь откладывает crash до `_get_target_metadata()` call.
- **S1 Suggestion** (scheduler.py:122): redundant `from bot.models import Booking` (повторно в line 138).

**Fixes applied + re-verify + re-review (усиление N):**

- **F1 fix** (scheduler.py): добавлен `.replace(tzinfo=UTC)` перед `.astimezone(tz)` + comment (mirror of booking.py:380). Regression test `test_send_reminder_timezone_utc_to_moscow` с `monkeypatch.setenv("TZ", "Europe/Moscow") + time.tzset()` — TZ-independent, ловит regression на CI (TZ=UTC) и dev (TZ≠UTC). Verified: без fix → "11:00" (fail), с fix → "14:00" (pass).
- **W1 fix** (scheduler.py): try/except `KeyError` (ZoneInfoNotFoundError subclass, PEP 615) вокруг `ZoneInfo(...)`. **ZoneInfo проверка ПЕРЕД log_notification** — если timezone invalid, return BEFORE INSERT → retry possible когда DBA починит timezone. `except KeyError` (не `Exception`) — не ловит MemoryError/AttributeError. Regression test `test_send_reminder_invalid_timezone_logs_error`.
- **W2 fix** (alembic/env.py): comment переписан — явно указано что lazy import лишь откладывает crash, не решает; BOT_TOKEN/ADMIN_ID всё ещё нужны; предложен Optional fields или separate config как реальный фикс.
- **S1 fix** (scheduler.py): удалён redundant import.

**Iter 2 (re-review): LGTM** — все 3 кодовых фикса корректны. 2 warnings (W1-PARTIAL — устранён переупорядочиванием, W2-TEST-TZ — устранён monkeypatch), 2 suggestions (S1-EXCEPT — упрощено до KeyError, S2-FREEZE — удалён dead freeze_time). Все применены в этом commit.

### Known limitations (Session 4 status)

| # | Limitation | Severity | Source | Status |
|---|---|---|---|---|
| L1 | `callback_query` fallback в mid-FSM после restart | Minor (UX) | Critic iter 1 Pass 3 | ✅ **Closed in Session 4** — `no_state_callback_fallback` (client.py:869), registered LAST in client_router |
| L2 | `log_notification → send_message` atomicity: bot crash между INSERT `notifications_log` и `bot.send_message` → сообщение потеряно (UNIQUE блокирует retry навсегда). MVP-допущение из `spec.md:396`. | Minor (data) | spec.md:396 | ⏳ **Open** — прод: двухфазная схема (INSERT с `sent_at=NULL`, UPDATE `sent_at=now()` после send + reaper для зависших) |
| L3 | N+1 в `get_overdue_bookings_without_remind_24h` | Minor (perf) | Critic iter 1 Pass 2 | ✅ **Closed in Session 4** — batched NOT EXISTS subquery, single query (notifications.py:46-64) |
| L4 | `_bot_ref` global mutable state | Code smell | Self-identified | ❌ **Dropped in Session 4** — ContextVar не работает с AsyncIOScheduler (jobs запускаются без caller context, verified). Глобал остаётся |
| L5 | FSM storage `MemoryStorage` теряется при Render restart | Major (UX, deferred from Session 2) | spec.md:538-547 | ✅ **Closed in Session 4** — custom PostgresStorage (bot/fsm_storage.py), env-based switch, JSONB atomic merge + 4 code-review fixes (W1+W2+W3+W4) |

### Cross-refs

- `~/.config/opencode/skills/deep-analysis-protocol/SKILL.md` — гейт протокол (Pass 1-4 + critic)
- `~/.config/opencode/AGENTS.md` § git-repo-categories — barber-bot = personal free (коммиты без переспроса)
- `~/.config/opencode/references/donor-research/topics/booking-bot-architecture.md` — BB-008 (EXCLUDE), BB-011 (SQLAlchemyJobStore), BB-012 (on_startup_scan), BB-013 (idempotency UNIQUE), BB-014 (anti-pattern MemoryJobStore)
- https://render.com/docs/postgresql-extensions — btree_gist availability VERIFIED (PG 13+)
- Spec: Урок 2.4 master handlers (spec.md:280-340 trigger'ы, 358-396 on_startup flow), Урок 2.6 Postgres migration (spec.md:320-447), 538-547 FSM storage

### NEXT: Session 5 — план

**Готово к ручному smoke test на Render (HIGH PRIORITY):**
1. **Render deploy smoke test** — после push `66e11e4`+`e3cf1dc` Render задеплоит автоматически. Проверить:
   - `/start` в Telegram → бот отвечает
   - `/book` → выбор даты/слота/имени/услуги → подтверждение → бронь создаётся
   - Master notification приходит на `settings.ADMIN_ID`
   - FSM state проверяет переживание restart: `/book` mid-flow → Render restart (15 min) → продолжить → state preserved (новый PostgresStorage, миграция 003 выполнена)
   - `/mybookings` → список → `[🚠 Отменить]` → отменяется → EXCLUDE constraint не срабатывает на той же дате/времени (UNIQUE slot_id fallback на SQLite, EXCLUDE USING gist на Postgres)
   - `[🔄 Перенести]` → выбор нового слота → перенос выполняется, старый slot освобождается
   - Bot restart mid-FSM → tap stale inline button → `no_state_callback_fallback` показывает popup "Сессия истекла — начните через /book"

**Code review S3 (Unit-тест Postgres JSONB path):**
2. **Smoke test PostgresStorage на Postgres** — unit-тесты в `test_fsm_storage.py` покрывают только SQLite path (read-modify-write). Postgres JSONB `||` atomic merge + ON CONFLICT DO NOTHING не exercised. Решение: либо testcontainers + pytest mark `@pytest.mark.postgres`, либо manual smoke на Render.

**Остальные limitations:**
3. **L2 atomicity** — двухфазная схема log_notification (INSERT с sent_at=NULL → UPDATE после send → reaper для зависших). Production-level, отложено в backlog.

**Урок 2.5+ — новые features:**
4. **Master handlers end-to-end на Render** — `/today` уже работает локально (admin.py:243), проверить на prod.
5. **Master notifications** — master_new/cancel/transfer УЖЕ реализованы в handlers (client.py:423-427, 609-613, 843-847), покрыты тестами (test_client_handlers.py — `bot.send_message.assert_called_once()` для всех 3 сценариев). Закрыто без новой работы.
