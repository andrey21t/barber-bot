# NEXT_SESSION_PROMPT — Session 5 закрыт (Render smoke через Timeweb VPS), Session 5.5 готов (Вариант B: inline admin menu + FSM).

> Дата: 2026-08-27 · Session 5 commit fda9263 + 4e90a9d (Docker на Timeweb VPS, 790₽/мес, бот @My_Barber_hair_bot живой). Session 5.5 commit f239774 (Этап 1.1 AdminStates + Этап 1.2 inline menu keyboards). Deep-analysis 2 итерации critic (SURFACE_LEVEL → INCOMPLETE по протоколу max 2). User выбрал Вариант C (малые шаги + verify+review). Code-reviewer LBTM (F1 docstring ложный) → fixed → LGTM. **Готов к Этапу 1.3 (handlers/admin.py — самый большой, разбит на 1.3a-e).**

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
Продолжаем barber-bot Session 5.7 — Этап 1.3b (FSM для addslots) с учётом
доноров, найденных в Session 5.6 (UznetDev/Aiogram-Bot-Template, INSIGHT INL-001
edit_message_text для inline навигации). Session 5.5/5.6 — inline admin menu
Варианта B в работе (Этап 1.3a завершён, points of entry в flow готовы, дальше —
FSM-логика после тапа по кнопке).

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный
контекст. Особое внимание:
- "Quick start prompt" ниже (этот блок)
- "Session 5.6 — доноры inline admin menu" ниже (новый раздел с INSIGHT)
- "Session 5.5 — план Варианта B" (план A, 21 находка critic, все этапы 1.3-5)

== СТАТУС ==
- Sessions 1-4.5 закоммичены и запушены (origin/main).
- Session 5 commit fda9263 + 4e90a9d: Dockerfile + docker-compose.yml +
  host network fix (Telegram IPv6 blocked в Docker bridge на Timeweb).
- Session 5.5 commit f239774: Этап 1.1 (AdminStates в states.py) + Этап 1.2
  (inline menu keyboards в keyboards/admin.py). Deep-analysis 2 итерации
  critic (SURFACE_LEVEL → INCOMPLETE по протоколу, max 2 iter). User выбрал
  Вариант C (малые шаги с verify+review после каждого файла). Code-reviewer
  LBTM (F1 docstring 'не показывается в /start' — ложно) → fixed → LGTM.
- Session 5.6 commit 4f996c7 + 228fcad (M1 fix): Этап 1.3a
  (_is_admin_callback + 5 menu callbacks в handlers/admin.py, ~577 строк).
  Code-reviewer LGTM (0 critical, 7 warnings, 1 fix applied W4 → M1 в нашей
  ревизии: assert вместо 'if user is not None else 0' полумеры).
  StateFilter("*") для menu callbacks (UX-edge прерывание FSM тапом).
  Today/week — НЕ трогают state (read-only peek без отмены текущего flow).
  Deferred warnings: W1 (services без resolve — KISS, business_id нужен в 1.3d),
  W2/W7 (try/except на DB query — отдельный проход), W3 (calendar-tap dangling —
  ожидаемо для 1.3a, добавит 1.3b/1.3c), W5 (callback.answer в конце — design
  choice match client.py), W6 (double-tap — acceptable MVP).
  НЕ ДЕПЛОИТЬ 1.3a без 1.3b+1.3c — calendar-tap dangling (W3).
- Session 5.6 доноры: UznetDev/Aiogram-Bot-Template (10 fork, aiogram 3.5)
  изучен через gh api raw. 5 INSIGHT (INL-001..005), см. раздел ниже.
  Карточка: ~/.config/opencode/references/donor-research/donors/UznetDev-aiogram-bot-template.md
  Topic:   ~/.config/opencode/references/donor-research/topics/inline-admin-menu-aiogram.md

== РАЗВЁРТКА НА TIMWEB VPS (живой, 790₽/мес) ==
- IP: 188.225.82.248, user: root, pass: hA+-PZrPCGmVV3
- Команды: ssh root@188.225.82.248
  cd /opt/barber-bot && git pull && docker compose down && docker compose up -d --build
  docker compose logs bot --tail 30 (логи)
  docker compose ps (статус)
- БД seeded: Business 'Barber Ekaterina' (id 04d1be59-422a-4f92-88bf-242031f14d82)
  + Master 'Ekaterina' telegram_id=461355056. /today, /week, /addslots УЖЕ
  работают на prod. Smoke test пройден частично (команды работают, FSM state
  survives restart НЕ проверяли — нужно: /book mid-flow → docker compose
  down → up → продолжить).
- Бот: @My_Barber_hair_bot (telegram), token 8935808150:AAEJOLoklDzQQrrmBH4KzHS2JphMa5uzIBQ
- .env на сервере: /opt/barber-bot/.env (BOT_TOKEN, ADMIN_ID=461355056,
  POSTGRES_USER=barber, POSTGRES_PASSWORD=ChangeMe123, POSTGRES_DB=barber)

== ДОНОРЫ SESSION 5.6 (UznetDev/Aiogram-Bot-Template, INSIGHT INL-001..005) ==

Полная карточка: ~/.config/opencode/references/donor-research/donors/UznetDev-aiogram-bot-template.md
Полный topic:    ~/.config/opencode/references/donor-research/topics/inline-admin-menu-aiogram.md

INL-001 · edit_message_text для inline навигации (UX pattern) · TODO 1.3b/1.3c ⏳
  Атрибуция: UznetDev/Aiogram-Bot-Template/handlers/admins/callback_query/main_admin_panel.py:30-34
  Паттерн: при навигации между разделами админ-панели ИЗМЕНЯТЬ существующее
  сообщение (edit_message_text / edit_reply_markup), не создавать новое (answer).
  Чат админа не засоряется — один message, кнопки меняют его content.
  Наш статус: НЕ применяем в 1.3a (там callback.message.answer создаёт новое
  сообщение с calendar). Для 1.3b/1.3c — ПРИМЕНИТЬ: при показе calendar после
  тапа по «➕ Открыть слоты» / «🔒 Закрыть слот» — заменить answer на
  edit_message_text (старое сообщение «Выберите дату» → новое «Выберите час» +
  новый keyboard). UX чище.
  Применимость: для addslots/closeslot/services (новый flow, новое сообщение ОК
  для calendar, но следующие шаги внутри flow — edit). Для today/week — НЕ
  трогать (read-only, уже закоммичено 4f996c7).

INL-002 · message_id cleanup в state · REJECT ❌ (нет pain)
  Атрибуция: UznetDev/Aiogram-Bot-Template/handlers/admins/main_panel.py:43-50
  Паттерн: сохранить message_id панели в state, при новом открытии — удалить
  старое сообщение. Чат не засоряется сообщениями панели между сессиями.
  Решение: НЕ внедрять. AGENTS.md § anti-overengineering правило 3 (5+ повторений
  = pain). Pet-project MVP, нет pain signal. Если админ жалуется на засорение —
  вернуться.

INL-003 · IsAdmin как BaseFilter · TODO Ур. 2.6 ⏳
  Атрибуция: UznetDev/Aiogram-Bot-Template/filters/admin.py:42-67
  Паттерн: auth как BaseFilter — ставится декоратором на handler, декларативно.
  Не нужно `if not _is_admin_callback(callback): await callback.answer(); return`
  в каждом handler.
  Наш статус: _is_admin(message) + _is_admin_callback(callback) — императивно,
  повторяется в 5 callbacks. Refactor в Ур. 2.6 (middleware/role.py уже в плане
  barber-bot NEXT_SESSION_PROMPT). Donor подтверждает паттерн BaseFilter — но
  наш план идёт дальше (middleware с DB lookup, не фильтр с settings.ADMIN_ID).

INL-004 · их баг с message.from_user для CallbackQuery · APPLIED ✅
  Атрибуция: UznetDev/Aiogram-Bot-Template/filters/admin.py:50-67
  Баг: IsAdmin.__call__(message: Message) проверяет message.from_user.id ==
  ADMIN. Для CallbackQuery message = callback.message (исходное сообщение бота),
  message.from_user = бот (кто отправил сообщение с кнопкой), НЕ тапер. Фильтр
  проверяет ID бота, не ID админа — фактически любой тапер проходит.
  Наш fix (commit 4f996c7): _is_admin_callback(callback) через callback.from_user
  (НЕ message.from_user). Мы сделали правильно. Donor верифицировал наш подход.

INL-005 · CallbackData с action field · REJECT ❌ (наш подход type-safer)
  Атрибуция: UznetDev/Aiogram-Bot-Template/keyboards/inline/button.py:8-15
  Паттерн: один CallbackData класс AdminCallback(action: str, data: str) для
  всех admin actions. Фильтр по F.action == "...". Компактнее для >5 buttons.
  Наш подход: 5 отдельных CallbackData (AdminAddslotsCallbackData,
  AdminCloseslotCallbackData, AdminTodayCallbackData, AdminWeekCallbackData,
  AdminServicesCallbackData), каждый со своим prefix. Type-safer, IDE autocomplete,
  легче добавить новый action (новый класс, не новое поле в существующем).
  Решение: НЕ менять. Для 5 buttons наш подход читаемее и type-safer. Если будет
  >10 admin actions — пересмотреть.

== ЧТО ОСТАЛОСЬ (Session 5.7 scope — Этап 1.3b, Вариант B) ==

ПЛАН A (расширенный после 2 итераций deep-analysis-critic):
- 21 находка critic (10 iter 1 + 11 iter 2) — все в плане, решаются в коде.
- Вариант C: 1 файл → verify (pytest+ruff+mypy) → code-review → следующий.

Этап 1.3 — bot/handlers/admin.py (САМЫЙ БОЛЬШОЙ, разбит на подэтапы):
  ✅ 1.3a (commit 4f996c7 + 228fcad M1 fix) — _is_admin_callback(callback) для
       auth CallbackQuery + 5 menu callback handlers (StateFilter("*") +
       admin_* filter) — точки входа в flow. Каждый: callback.answer() +
       state.clear() + state.set_state(AdminStates.<first_state>) + показать
       calendar/ask. Today/week — read-only, state НЕ трогают.
       НЕ ДЕПЛОИТЬ без 1.3b+1.3c — calendar-tap dangling (W3).
  ⏳ 1.3b — FSM для addslots (AdminStates.adding_slots_date → adding_slots_hours):
       SimpleCalendar handler с StateFilter(AdminStates.adding_slots_date) —
       НОВЫЙ, текущий в client.py:213 привязан к BookingStates.selecting_date, НЕ
       сработает для admin-state без правки. После выбора даты → ask hours
       → parse + add_slots service → "✅ Слоты созданы" (re-use логики из
       cmd_addslots:147-165). NB INL-001: для показа шага «ask hours» после
       тапа по дате calendar — ИСПОЛЬЗОВАТЬ edit_message_text (не answer),
       чат админа не засоряется. Calendar tap → edit_message_text с ask hours
       + (если нужно) reply keyboard с часами ИЛИ text input.
  ⏳ 1.3c — FSM для closeslot (AdminStates.closing_slot_date → closing_slot_hour):
       аналогично addslots. После выбора даты → ask hour (single, не multiple)
       → close_slot service → "✅ Слот закрыт". NB INL-001: edit_message_text
       при переходе calendar → ask hour.
  ⏳ 1.3d — FSM для services (3 шага: name → duration → price):
       AdminStates.entering_service_name → _duration → _price.
       Name с пробелами ОК (create_service делает name.strip(), сохраняет
       внутренние пробелы — не как cmd_services с replace('_', ' ')).
       NB INL-001: edit_message_text между шагами (name → duration → price),
       чат не засоряется. NB W1 code-review 1.3a: добавить _resolve_master_and_business
       на entry (business_id нужен для create_service в конце flow).
  ⏳ 1.3e — /cancel с StateFilter(AdminStates) в admin_router (перехватывает
       раньше client_router — router order main.py:117-119). +
       admin-state catch-all:
       - text: StateFilter(AdminStates), F.text, ~F.text.startswith("/")
         → "Используйте /cancel для отмены" (иначе бот молчит — критик D, E)
       - callback: StateFilter(AdminStates) + не-команда callback
         → "Используйте /cancel"
       Порядок регистрации внутри admin_router: specific → /cancel → catch-all.

Этап 2 — middleware + spec + docstrings:
  2.1 — bot/middlewares/session_timeout.py:
        + ADMIN_SESSION_TTL_SEC = 3600 (60 мин для админа, парикмахер
          отвлекается на клиентов — 30 мин мало).
        В __call__: if event.from_user.id == settings.ADMIN_ID:
          ttl = ADMIN_SESSION_TTL_SEC else ttl = SESSION_TTL_SEC.
        Текст оставить клиентский ("/book hint") — acceptable UX, не трогать
        test_middlewares.py:291 assert "/book" in text.
        test_middlewares.py:377 assert SESSION_TTL_SEC == 1800 — оставить
        (клиентский), +новый тест на ADMIN_SESSION_TTL_SEC == 3600.
  2.2 — spec.md правка 6+ точек (SSOT: "прав spec, не код"):
        - spec.md:247 — добавить AdminStates
        - spec.md:251 — inline menu вместо reply
        - spec.md:255 — handlers/admin.py: callback + FSM
        - spec.md:404 — команды как alias
        - spec.md:405-410 — parsing-правила → FSM validate на каждом шаге
        - spec.md:458 — checklist обновить под inline-flow
        - spec.md:491 — /cancel контракт для admin-FSM
        - spec.md:513 — ADMIN_SESSION_TTL_SEC = 3600
        - + НОВАЯ точка: spec.md добавить INL-001 (edit_message_text для
          inline навигации в admin FSM — UX паттерн, чат не засоряется).
  2.3 — docstrings/comments:
        - bot/handlers/admin.py:1-14 — уже "Stateful: PARTIAL" (commit 4f996c7),
          при 1.3b обновить до "Stateful: YES" + "5 callback + FSM"
        - bot/main.py:115 — comment про /cancel в admin-FSM

Этап 3 — bot/handlers/start.py:
  - Welcome для админа: убрать перечисление /addslots /closeslot /today /week
    /services из текста. Прикрепить admin_inline_menu() (inline keyboard).
  - Правка tests/test_start_handlers.py:60-64 asserts (5 asserts на /addslots
    in text → упадут). Решение: переписать asserts на inline keyboard.

Этап 4 — тесты:
  4.1 — Правка существующих:
        - tests/test_start_handlers.py:48-68 — asserts на inline keyboard
        - tests/test_admin_handlers.py — 54 теста остаются (commands как alias)
        - tests/test_middlewares.py:377 — оставить, +новый на ADMIN_SESSION_TTL_SEC
  4.2 — Новые тесты в tests/test_admin_handlers.py:
        - Menu callback — каждая из 5 кнопок триггерит правильный flow
        - FSM addslots — календарь → выбор часа → создание
        - FSM closeslot — календарь → выбор часа → закрытие
        - FSM services — 3 шага (name/duration/price)
        - /cancel в admin-FSM → state cleared + admin text
        - admin-state fallback (text + callback)
        - _is_admin_callback (админ vs не-админ)
        - TTL=60 для админа (middleware test)
        - INL-001: edit_message_text вызывается (not answer) при переходе
          calendar → ask hours в 1.3b/1.3c

Этап 5 — verify + deploy:
  5.1 — qa-verify-and-fix (локально): pytest + ruff + mypy, итеративный fix
  5.2 — qa-code-review через code-reviewer subagent
  5.3 — git commit + push (pet-project free)
  5.4 — Deploy на Timeweb: ssh root@188.225.82.248 → git pull → docker compose
        down → up -d --build
  5.5 — Smoke test в Telegram (@My_Barber_hair_bot):
        - Екатерина жмёт /start → видит inline menu (5 кнопок)
        - Тапает "➕ Открыть слоты" → календарь → дата → ввод часов → слоты созданы
        - Тапает "📅 Сегодня" → мгновенный список записей
        - Тапает "💇 Добавить услугу" → 3 шага → услуга создана
        - /cancel в mid-FSM → корректный выход
        - 30+ мин бездействия → middleware даёт TTL=60 для админа
        - Bot restart mid-admin-FSM → PostgresStorage сохраняет,
          admin-state fallback ловит stale taps

== 21 НАХОДКА CRITIC (памятка — все решаются в коде, не "отложены") ==
Блокирующие (3):
  - test_middlewares.py:377 SESSION_TTL_SEC == 1800 — оставить (клиентский),
    +ADMIN_SESSION_TTL_SEC = 3600 → Этап 2.1
  - test_middlewares.py:291 "/book" in text — оставить клиентский текст
    для админа (acceptable UX) → Этап 2.1
  - StateFilter(AdminStates.*) синтаксис невалиден → правильно
    StateFilter(AdminStates) (verified через python -c) → Этап 1.3e
Дополнения (8):
  - spec.md 6+ точек → Этап 2.2
  - admin.py docstring (line 6 "Stateful: NO") → Этап 2.3 (уже "PARTIAL" в 4f996c7)
  - main.py:115 comment → Этап 2.3
  - UX-edge прерывание FSM тапом → ✅ Этап 1.3a (state.clear() + новый flow)
  - SimpleCalendar для админа → ⏳ Этап 1.3b (новый handler)
  - Auth для callbacks → ✅ Этап 1.3a (_is_admin_callback)
  - /cancel клиентский текст для админа → ⏳ Этап 1.3e (admin-state /cancel)
  - TTL=60 для админа → ⏳ Этап 2.1
Остатки (10):
  - catch-all filter exclude commands → 1 строка ~F.text.startswith("/") → 1.3e
  - persistence inline keyboard → StateFilter("*") + admin_* handler → ✅ 1.3a
  - dispatcher order внутри router → specific → /cancel → catch-all LAST → 1.3e
  - admin-state callback fallback 2 уровня → StateFilter("*")+admin_* для menu,
    StateFilter(AdminStates) catch-all для in-FSM → ✅ 1.3a + ⏳ 1.3e
  - Bot restart mid-admin-FSM → PostgresStorage (уже L5) + admin-state fallback → 1.3e
  - Double-tap inline → events_isolation сериализует + idempotent set_state → accept ✅
  - /services name с пробелами в FSM → message.text принимает полное → 1.3d
  - test_start_handlers правка → Этап 4.1
  - test_middlewares правка → Этап 4.1
  - test_admin_handlers 54 теста → оставить как alias → Этап 4.1

== ГЕЙТЫ (напоминание) ==
- qa-verify-and-fix после правок кода (.venv/bin/python -m pytest + ruff + mypy)
- qa-code-review через code-reviewer subagent на logic-change
- Вариант C: 1 файл → verify → code-review → следующий файл
- Коммиты свободно (pet-project, AGENTS.md § git-repo-categories)
- Push в origin/main после verify + code-review

== NEXT ==
Этап 1.3b — FSM для addslots (AdminStates.adding_slots_date → adding_slots_hours).

ЧТО ДЕЛАТЬ (пошагово):
1. Прочитай bot/handlers/admin.py (~577 строк после 1.3a + M1 fix, callbacks в
   конце с 443 строки). Контекст: admin_addslots_cb (443) уже стартует flow —
   state.clear() + state.set_state(AdminStates.adding_slots_date) + показывает
   calendar через admin_calendar_keyboard. После тапа по дате — НЕТ handler
   (W3 code-review 1.3a — calendar-tap dangling). Это Этап 1.3b закрывает.
2. Прочитай bot/handlers/client.py:124-211 (_handle_simple_calendar — паттерн
   для SimpleCalendar handler). Особенно строки 158-194 (act=day branch —
   обработка выбора даты). Текущий simple_calendar_cb (213) привязан к
   BookingStates.selecting_date через StateFilter — НЕ сработает для
   AdminStates.adding_slots_date. Нужен НОВЫЙ handler в admin.py.
3. Прочитай bot/handlers/client.py:77-94 (_calendar_range — паттерн для
   range, у нас _admin_calendar_range уже есть в admin.py:429-440).
4. Прочитай cmd_addslots в admin.py:121-190 — там parsing часов + add_slots
   service + render результата. Это RE-USE для 1.3b (после выбора даты → ask
   hours → parse → add_slots → "✅ Слоты созданы").

РЕАЛИЗАЦИЯ 1.3b (новый код в admin.py, после admin_addslots_cb ~470 строки):
  - Новый handler admin_addslots_calendar_cb:
    @router.callback_query(SimpleCalendarCallback.filter(),
                           StateFilter(AdminStates.adding_slots_date))
    async def admin_addslots_calendar_cb(callback, callback_data, state):
        # Аналог _handle_simple_calendar, но:
        # - act=day: сохранить selected_date в state, set_state(adding_slots_hours),
        #   INL-001: edit_message_text (НЕ answer) "Введите часы через пробел
        #   (например 11 12 13):" — чат не засоряется
        # - act=cancel: state.clear() + edit_message_text "Отменено"
        # - act=navigation: edit_reply_markup (lib делает, handler answer)
        # - act=ignore/today: callback.answer(cache_time=60)
  - Новый handler admin_addslots_hours_msg:
    @router.message(StateFilter(AdminStates.adding_slots_hours))
    async def admin_addslots_hours_msg(message, state):
        # parse hours (re-use cmd_addslots parsing:121-152)
        # resolve master (re-use _resolve_master_and_business)
        # validate slot_date not in past (re-use cmd_addslots:165-170)
        # add_slots service (re-use cmd_addslots:172-180)
        # render "✅ Открыты слоты на DATE: HOURS" (re-use cmd_addslots:182-190)
        # state.clear() в конце (flow завершён)

ГЕЙТЫ 1.3b:
- qa-verify-and-fix: .venv/bin/ruff check + .venv/bin/mypy + .venv/bin/python -m pytest
- qa-code-review через code-reviewer subagent (logic-change, новый FSM flow)
- Вариант C: 1 файл (admin.py) → verify → code-review → commit
- Коммит: "Этап 1.3b: FSM addslots (calendar handler + hours input)"

DONOR CROSS-REF (INL-001 — ПРИМЕНИТЬ в 1.3b):
  - ~/.config/opencode/references/donor-research/topics/inline-admin-menu-aiogram.md
  - INL-001: edit_message_text вместо answer при переходе calendar → ask hours.
    Атрибуция: UznetDev/Aiogram-Bot-Template/handlers/admins/callback_query/main_admin_panel.py:30-34
    Паттерн: при навигации между разделами админ-панели ИЗМЕНЯТЬ существующее
    сообщение, не создавать новое. Чат админа не засоряется.
    Применение: в admin_addslots_calendar_cb (act=day branch) —
    callback.message.edit_text("Введите часы...") вместо callback.message.answer().
    Если message нельзя edit (старое, удалено) — fallback на answer.

NB: НЕ ДЕПЛОИТЬ без 1.3b+1.3c — иначе calendar-tap dangling (W3 code-review 1.3a).
```

## Session 5.6 — доноры inline admin menu (UznetDev/Aiogram-Bot-Template, INSIGHT INL-001..005)

> Контекст: пользователь спросил «почему мы выдумываем велосипед — наверняка есть
> готовые inline admin menu решения». Локальная база donor-research (2026-07-18
> booking-bot-architecture, BB-001..BB-014) покрывала архитектуру БД/services/
> booking FSM, НЕ admin UX (inline menu, callback auth, FSM для админа).
> Cascade level 0 через `gh search repos "aiogram admin panel"` (PRIMARY,
> AGENTS.md § Cascade). Из 50 `gh search repos "aiogram"` релевантных по
> описанию (admin/booking/menu/salon/barber/schedule/master) — 2. Дальнейший
> поиск `gh search repos "aiogram admin inline menu"` / `"aiogram FSM admin"` /
> `"telegram booking"` → 30+ результатов, проверены топ-7.

### Принятый донор: UznetDev/Aiogram-Bot-Template

- URL: https://github.com/UznetDev/Aiogram-Bot-Template
- Fork: 10, обновлён 2026-08 (active), stack: aiogram 3.5 + MySQL + Cython
- Полноценный admin panel: `/admin` command → inline menu 5 кнопок (admins
  settings, send advertisement, statistika, check user, channel setting),
  role-based access (super admin + regular admins с permissions), FSM
  AdminState(StatesGroup) с 5 states, CallbackData с action field,
  multi-language (translator + user.language_code).

### Отклонённые доноры (cascade level 0 — искал, не подошли)

| Донор | Причина отклонения |
|---|---|
| shoxauk2006-ops/boockly | Telegram WebApp launcher (bot.py = 30 строк). Не inline FSM для админа. |
| kartavyj44/telegram-booking-bot | Очень простой (88 строк), inline только для клиента /book. Админ через /bookings командой. |
| innetaccess/telegram-booking-bot | python-telegram-bot (НЕ aiogram). Owner notifications через send_message, не inline menu. |
| SatoBIGBrain1/telegram-booking-bot | python-telegram-bot + Dash dashboard. ReplyKeyboard (НЕ inline). ConversationHandler (PTB's FSM). |
| Doc-Duck/AiogramAdminPanel | Пустой репозиторий (только __init__.py). |
| AlexaSidash/telegram-booking-bot | web_app.py — фронтенд-приложение, не бот. |
| TimonMonster/Telegram-booking-bot | C# (не Python). |

### 5 INSIGHT (INL-001..005)

| # | Что | Статус | Где решается |
|---|---|---|---|
| INL-001 | `edit_message_text` для inline навигации (UX — чат не засоряется) | ⏳ todo | 1.3b/1.3c (показ calendar → ask hours) |
| INL-002 | `message_id` cleanup в state (удалять старые сообщения панели) | ❌ reject | нет pain (AGENTS.md § anti-overengineering правило 3) |
| INL-003 | `IsAdmin` как `BaseFilter` вместо хелпера | ⏳ todo | Ур. 2.6 (middleware/role.py уже в плане) |
| **INL-004** | **их баг с `message.from_user` для CallbackQuery = бот, НЕ тапер** | ✅ **applied** | наш fix `_is_admin_callback(callback)` через `callback.from_user` в Этап 1.3a commit 4f996c7 — donor верифицировал |
| INL-005 | `CallbackData` с `action` field (один класс на все actions) | ❌ reject | для 5 buttons наш подход (5 CallbackData) type-safer, не менять |

### Что НЕ applies к нам (reject with rationale)

- **MySQL** (мы PostgreSQL + SQLAlchemy async) — donor использует mysql-connector-python (sync).
- **Cython оптимизация** (`cython_code/`) — overkill для pet-project barber-bot. Donor оптимизирует send_ads (100 msg/min) и translator. У нас single-master, нет mass mailing.
- **translator + multi-language** — наш single-master single-language (русский). Donor переводит UI по `user.language_code`.
- **Role-based permissions через DB** (`SelectAdmin` с 6 методами) — у нас hardcoded `settings.ADMIN_ID` (MVP single-master). Ур. 2.6: middleware/role.py с DB lookup (уже в плане barber-bot NEXT_SESSION_PROMPT.md).
- **Mandatory channels** (проверка подписки на каналы) — не наш кейс (барбер не требует подписку на канал).
- **Send advertisement** (mass mailing 100 msg/min) — не наш кейс (single-master, нет рекламы).

### Итог

barber-bot Этап 1.3a (commit 4f996c7 + 228fcad M1 fix) — валиден. Donor UznetDev/
Aiogram-Bot-Template подтвердил наш паттерн (FSM states для admin flow, CallbackData
для inline buttons). Их баг с `message.from_user` для CallbackQuery (INL-004) —
верифицировал наш fix `_is_admin_callback(callback)` через `callback.from_user`.

**Применяемые INSIGHT для следующих этапов:**
- INL-001 (edit_message_text для inline навигации) — Этап 1.3b/1.3c (показ calendar → ask hours)
- INL-003 (IsAdmin как BaseFilter) — Ур. 2.6 (middleware/role.py)

**Method note:** aiogram-ботов с inline admin panel на github мало — нишевая
тема. Наш 1.3a не «велосипед», а обоснованная реализация с refactor в плане (Ур. 2.6).

### Файлы донора (для справки, прочитаны через gh api raw)

Полные карточки:
- `~/.config/opencode/references/donor-research/donors/UznetDev-aiogram-bot-template.md`
- `~/.config/opencode/references/donor-research/topics/inline-admin-menu-aiogram.md`
- INDEX обновлён: `~/.config/opencode/references/donor-research/INDEX.md` (2026-08-27 запись)

## Cross-refs

- `~/.config/opencode/skills/deep-analysis-protocol/SKILL.md` — гейт протокол (Pass 1-4 + critic)
- `~/.config/opencode/AGENTS.md` § git-repo-categories — barber-bot = personal free (коммиты без переспроса)
- `~/.config/opencode/references/donor-research/topics/booking-bot-architecture.md` — BB-008 (EXCLUDE constraint), BB-011 (SQLAlchemyJobStore), BB-012 (on_startup_scan), BB-014 (anti-pattern MemoryJobStore на Render)
- `~/.config/opencode/references/donor-research/topics/inline-admin-menu-aiogram.md` — INL-001..005 (доноры для inline admin menu в aiogram 3.x, Session 5.6)
- `~/.config/opencode/references/donor-research/donors/UznetDev-aiogram-bot-template.md` — карточка донора (10 fork, aiogram 3.5, admin panel)
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

## NEXT: Session 4.5 — итоги (commits `38f5dc2` + `559ccbe` + `357b616`, запушены)

> Session 4.5: forensic review [REDACTED-SESSION-ID] → Findings 4-7 (refactor) + Postgres SQL construction tests + LBTM re-review.

### Что сделано в Session 4.5

**Commit `38f5dc2` — refactor(fsm): Findings 4-7 from session fd072656 review**
- **F4**: `alembic/versions/003_fsm_storage.py:60` — SQLite path `sa.TIMESTAMP` → `sa.DateTime(timezone=True)` (cross-DB consistency с models.py:234, Postgres path уже имел timezone=True)
- **F5**: `bot/fsm_storage.py` — кеш `dialect_name` в `__init__` через `session_factory.kw["bind"].dialect.name` (verified: `async_sessionmaker` хранит bind в `self.kw` dict, НЕ в `.bind` property). Заменены ВСЕ deprecated `session.get_bind()` calls (SQLAlchemy 2.0 deprecation).
- **F6**: `bot/fsm_storage.py` `_upsert_state` — убран лишний `SELECT data` перед `pg_insert.on_conflict_do_update`. PostgreSQL сохраняет columns NOT in SET — data column не трогается на UPDATE, INSERT нового row использует `data={}` (server_default). 1 query saved.
- **F7**: `bot/fsm_storage.py` `update_data` Postgres — заменён 2-step race-prone flow (UPDATE→None→INSERT ON CONFLICT DO NOTHING → return data_dict.copy()) на SINGLE atomic `pg_insert.on_conflict_do_update` с JSONB `data || excluded.data` merge через `insert_stmt.excluded`. Race-free single statement, returns merged JSONB from RETURNING.
- Удалён dead code `_insert_if_absent` (стал unused после F7) + unused import `update` из sqlalchemy.

**Commit `559ccbe` — test(fsm): Postgres path SQL construction tests — 100% coverage**
- Новый файл `tests/test_fsm_storage_postgres.py` (15 тестов) — покрывает все Postgres branches в `PostgresStorage` (было 0%, стало 100% line coverage на `bot/fsm_storage.py`).
- Подход: SQL construction unit-testing без testcontainers — `PostgresStorage(dialect_name="postgresql")` с mock AsyncSession, перехват SQLAlchemy statement из `session.execute`, компиляция с postgresql dialect, assertions на SQL keywords (ON CONFLICT, DO UPDATE, EXCLUDED, JSONB `||`, RETURNING).
- Honest limitation: runtime JSONB `||` merge semantics, ON CONFLICT target matching, RETURNING population — НЕ exercised (needs testcontainers Postgres ИЛИ Render smoke).

**Commit `357b616` — fix(tests): LBTM re-review — tautological assertions replaced**
- Code-reviewer subagent (review commit `559ccbe`) → **VERDICT: LBTM** с 2 Critical + 3 Warnings — все assertions были no-op (false confidence):
  - **F1 Critical** `test_update_data_postgres_returns_merged_dict:220` — `assert "extra" not in (...) or True` → assertion тавтологически True, defensive copy НИКОГДА не проверялся.
  - **F2 Critical** `test_set_state_postgres_sql_updates_only_state_and_updated_at:281-282` — `"set data"` (с пробелом) searched in `set_clause_lower.replace(" ", "")` (пробелы удалены) → тавтологически absent. `'set"data"'` (с кавычками) для unquoted non-reserved column `data` → тавтологически absent.
  - **W1+W3 Warnings** — merge direction `data || excluded.data` НЕ верифицирован (assertions `excluded` + `||` + `data` независимы, проходят при wrong order).
  - **W2 Warning** — `"setstate"` partial check (ловил state только как FIRST column).
- Fixes: убран `or True` (F1), новый helper `_set_columns()` regex parser для SET clause (F2+W2), substring assertion на exact merge expression `fsm_states.data || excluded.data` (W1+W3).
- **Mutation tests** (verify assertions реальные, не симуляция):
  - F1: `return dict(merged)` → `return merged` → F1 assertion fails ✓
  - F2: add `"data": {}` to SET `_upsert_state` → F2 assertion fails ✓
  - W3: swap merge direction `excluded.data.op("||")(FsmState.data)` → W3 assertion fails ✓
- **Re-review (усиление N)**: code-reviewer subagent → **VERDICT: LGTM**, no new findings. 1 Suggestion (S1 — assertion зависит от whitespace rendering `op(||)`, low risk, не блокирующее).

### Verify gate (Session 4.5)

- pytest: **266 passed** (was 251, +15 Postgres tests)
- ruff: clean
- mypy: clean
- coverage: **100% на bot/fsm_storage.py** (87 stmts, 0 miss)
- Mutation tests: 3/3 pass (assertions реальные)

### Known limitations (Session 4.5 status — обновление таблицы Session 4)

| # | Limitation | Severity | Source | Status (was) | Status (now) |
|---|---|---|---|---|---|
| L1 | callback_query fallback | Minor (UX) | Critic iter 1 | ✅ Closed Session 4 | unchanged |
| L2 | log_notification atomicity | Minor (data) | spec.md:396 | ⏳ Open | ⏳ Open (Session 5 candidate) |
| L3 | N+1 in overdue query | Minor (perf) | Critic iter 1 | ✅ Closed Session 4 | unchanged |
| L4 | _bot_ref global | Code smell | Self-identified | ❌ Dropped Session 4 | unchanged |
| L5 | FSM MemoryStorage | Major (UX) | spec.md:538-547 | ✅ Closed Session 4 | unchanged |
| **L6** | **Postgres JSONB runtime coverage** | **Honest limitation** | **Code-review S1** | **(new)** | **⏳ Open — SQL construction covered (100% line), runtime needs testcontainers/Render smoke** |
| **L7** | **Tautological assertions (false confidence)** | **Critical (test quality)** | **Code-review F1+F2** | **(new)** | **✅ Closed Session 4.5 — _set_columns regex parser + substring assertion + mutation tests** |

### Cross-refs (Session 4.5)

- `~/.config/opencode/AGENTS.md` § write-actions-subagents rule 7 — code-review gate, усиление N (re-review after LBTM)
- `~/.config/opencode/AGENTS.md` § git-repo-categories — barber-bot = personal free (коммиты без переспроса)
- `~/.config/opencode/skills/qa-code-review/SKILL.md` — POST-код semantic гейт, BP-10 A2 verification
- `tests/test_fsm_storage_postgres.py` — 15 Postgres SQL construction tests (commit 559ccbe + fixes 357b616)
- `tests/test_fsm_storage.py` — 17 SQLite path tests (не менялся в Session 4.5)

### NEXT: Session 5 — план (обновлён после Session 4.5)

**Готово к ручному smoke test на Render (HIGH PRIORITY — не менялся):**
1. **Render deploy smoke test** — после push `357b616` (последний) Render задеплоит автоматически. Проверить:
   - `/start` в Telegram → бот отвечает
   - `/book` → выбор даты/слота/имени/услуги → подтверждение → бронь создаётся
   - Master notification приходит на `settings.ADMIN_ID`
   - FSM state проверяет переживание restart: `/book` mid-flow → Render restart (15 min) → продолжить → state preserved (новый PostgresStorage, миграция 003 выполнена)
   - `/mybookings` → список → `[🚠 Отменить]` → отменяется → EXCLUDE constraint не срабатывает на той же дате/времени (UNIQUE slot_id fallback на SQLite, EXCLUDE USING gist на Postgres)
   - `[🔄 Перенести]` → выбор нового слота → перенос выполняется, старый slot освобождается
   - Bot restart mid-FSM → tap stale inline button → `no_state_callback_fallback` показывает popup "Сессия истекла — начните через /book"
   - PostgresStorage JSONB merge runtime: `/book` → write FSM state → Render restart → state read (verify JSONB `||` atomic merge работает на реальном Postgres, не только SQL construction)

**Остальные limitations (Session 5 candidates):**
2. **L2 atomicity** (production-level, materially changes) — двухфазная схема log_notification: INSERT с `sent_at=NULL` → UPDATE `sent_at=now()` после успешного `send_message` → reaper для зависших (sent_at=NULL старше N минут). Меняет contract `notifications_log` (sent_at становится nullable, добавляется reaper job в scheduler). Нужен deep-analysis (logic change + state machine).
3. **L6 Postgres JSONB runtime** (Honest limitation) — SQL construction покрыт (15 тестов, 100% line coverage), runtime JSONB `||` merge / ON CONFLICT target / RETURNING population НЕ exercised. Опции: (a) testcontainers + `@pytest.mark.postgres`, (b) Render smoke (покроет п.1), (c) оставить как Honest limitation (pragmatic для pet). Рекомендация: Render smoke (п.1) закроет runtime coverage без new deps.

**Урок 2.5+ — новые features (не менялся):**
4. **Master handlers end-to-end на Render** — `/today` уже работает локально (admin.py:243), проверить на prod.
5. **Master notifications** — master_new/cancel/transfer УЖЕ реализованы в handlers (client.py:423-427, 609-613, 843-847), покрыты тестами (test_client_handlers.py — `bot.send_message.assert_called_once()` для всех 3 сценариев). Закрыто без новой работы.
