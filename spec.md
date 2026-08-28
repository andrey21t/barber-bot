# barber-bot — spec.md

> Источник правды. AI читает spec.md перед каждой задачей.
> Если код расходится со spec — прав spec, не код.
> Дата: 2026-07-18 · Автор: Андрей (vibe-coding Уровень 2)

---

## One-sentence description

Telegram-бот для записи к парикмахеру-одиночке: мастер управляет слотами вручную (плавающий график), клиенты выбирают свободные слоты через inline-кнопки, напоминания за 24ч и 1ч.

## Контекст (почему делаем)

Екатерина (парикмахер, Мурино) ведёт расписание вручную в чате:
- Пишет пост "Расписание на неделю" с датами и списком свободных слотов
- Клиенты пишут ей в личку "запишите Пашу на стрижку во вторник"
- Она отвечает "во сколько?" → клиент называет время → "хорошо"
- Боли: "вы не отвечаете 😊" (не успевает отвечать), двойные записи, забытые напоминания

Цель: презентовать ей бота бесплатно → отзыв + скринкаст в портфолио → кворк на Kwork "Telegram-бот для записи к мастеру под ключ" (чек 1500–3000₽).

## MVP scope

### В MVP (первая версия)

1. **Мастер** управляет слотами вручную (график плавающий, не шаблонный):
   - `/addslots 2026-03-17 11 12 13 14 15` — открыть слоты на день
   - `/closeslot 2026-03-17 14` — закрыть слот (например, для окрашивания на 2 часа)
   - `/today` — записи на сегодня
   - `/week` — записи на неделю
    - `/services add <name> <duration_min>` — добавить услугу (для snapshot в booking)

2. **Клиент** записывается через inline-кнопки (FSM, БЕЗ name/phone в начале):
   - `/start` → кнопка "Записаться"
   - Выбор даты (inline-календарь `aiogram_calendar`)
   - Выбор слота (только свободные, inline-кнопки)
   - Ввод имени клиента (`client_name_snapshot` — "Паша"/"я сам"/"сын 5 лет")
   - Ввод услуги (свободный текст в MVP, БЕЗ выбора из списка — иначе нужно CRUD услуг мастером)
   - Подтверждение (✅ Записаться / ❌ Отмена)
   - `/mybookings` → отмена (>24ч) или перенос (>24ч)
   - `/cancel` — отмена записи

3. **Уведомления**:
   - Клиент ← напоминание за 24ч до записи
   - Клиент ← напоминание за 1ч до записи
   - Мастер ← новая запись
   - Мастер ← отмена записи
   - Мастер ← перенос записи
   - Все через `notifications_log` с UNIQUE(booking_id, kind) — idempotency guard

### Deferred (отложено, в roadmap.md)

- Multi-slot бронь авто (окрашивание = 2 слота автоматически) — пока мастер закрывает соседний слот вручную через `/closeslot`
- Авто-генерация слотов по шаблону графика (schedule-as-template) — не наш кейс (график плавающий)
- Выбор услуги из списка (пока свободный текст + snapshot)
- Цены / прайс-лист клиенту (мастер видит в /today, клиент — нет в MVP)
- SMS-уведомления (только Telegram)
- Блокировка no-show клиентов (отметить, заблокировать после 3 no-show)
- Multi-master (когда наймёт ассистента — INSERT в masters, 0 правок в handlers)
- Multi-location (разные TZ — INSERT в businesses с другим timezone, 0 правок в коде)
- Mini App (Telegram WebApp) — сал_bot паттерн, post-MVP
- Платежи (Stripe / YooMoney)
- White-label (Profile pattern) — когда 5+ клиентов

## Stack

- **Python 3.12**
- **aiogram 3.x** (НЕ 2.x — см. `.cursor/rules/aiogram-anti-hallucination.mdc`)
- **SQLAlchemy 2.0 async** + **Alembic** (миграции)
- **aiosqlite** (dev) → **asyncpg** (prod, Postgres на Render free tier)
- **APScheduler 3.x**: AsyncIOScheduler + SQLAlchemyJobStore (Postgres в проде, MemoryJobStore + on-startup scan в dev SQLite)
- **aiogram_calendar** (inline календарь для выбора даты)
- **pydantic-settings** (env config)
- **pytest + pytest-asyncio** (тесты)
- **opencode** (IDE, vibe-coding Уровень 2 — НЕ Cursor)

## Схема БД (7 таблиц, forward-compat к multi-master)

> **Реализация UUID (2026-08-23, Урок 2.6):** spec описывает `DEFAULT gen_random_uuid()`
> (server-side). Реализация использует **Python-side `default=uuid.uuid4`** (bot/models.py)
> — простота, работает на обеих БД (SQLite не имеет `gen_random_uuid`). На Postgres UUID
> генерируется в app перед INSERT, не в DB. Итог: pet-project simplicity > strict spec.
> alembic/versions/001_initial.py не задаёт server_default для UUID PK — Python-side.

```sql
-- 1. Бизнес (1 запись на старте = Екатерина, N при multi-location)
CREATE TABLE businesses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    telegram_owner_id BIGINT NOT NULL,
    timezone        VARCHAR(50) NOT NULL DEFAULT 'Europe/Moscow',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Мастера (1 запись на старте = owner, N при multi-master)
CREATE TABLE masters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID NOT NULL REFERENCES businesses(id),
    name            VARCHAR(255) NOT NULL,
    telegram_id     BIGINT,  -- nullable: master может не иметь TG
    role            VARCHAR(50) NOT NULL DEFAULT 'barber',  -- barber | owner
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_masters_business ON masters(business_id) WHERE is_active = TRUE;

-- 3. Услуги (привязаны к бизнесу)
CREATE TABLE services (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     UUID NOT NULL REFERENCES businesses(id),
    name            VARCHAR(255) NOT NULL,
    duration_minutes INT NOT NULL CHECK (duration_minutes > 0),
     price           NUMERIC(10,2),  -- nullable с Session 5.10 (мастер озвучивает цену в чате)
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_services_business ON services(business_id) WHERE is_active = TRUE;

-- 4. Клиенты (авторегистрация по telegram_id)
CREATE TABLE clients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id     BIGINT NOT NULL UNIQUE,
    name            VARCHAR(255),
    phone           VARCHAR(20),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. Слоты (materialized, master создаёт через /addslots)
-- slot_hour — ЛОКАЛЬНЫЙ час в business.timezone (master вводит /addslots 14 = 14:00 МСК)
-- При INSERT booking: start_at = datetime(slot_date, slot_hour, tz=ZoneInfo(business.timezone)).astimezone(UTC)
-- При /today мастеру: render slot_hour в business.timezone (НЕ UTC)
CREATE TABLE slots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    master_id       UUID NOT NULL REFERENCES masters(id) ON DELETE CASCADE,
    slot_date        DATE NOT NULL,
    slot_hour        SMALLINT NOT NULL CHECK (slot_hour BETWEEN 0 AND 23),
    status          VARCHAR(20) NOT NULL DEFAULT 'open',  -- open | booked | closed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (master_id, slot_date, slot_hour)
);
CREATE INDEX idx_slots_master_date ON slots(master_id, slot_date) WHERE status = 'open';

-- 6. Брони (с UNIQUE(slot_id) для SQLite + EXCLUDE constraint для Postgres)
CREATE TABLE bookings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slot_id          UUID NOT NULL REFERENCES slots(id),
    business_id     UUID NOT NULL REFERENCES businesses(id),
    master_id       UUID NOT NULL REFERENCES masters(id),
    client_id       UUID NOT NULL REFERENCES clients(id),
    service_id      UUID,  -- nullable в MVP (услуга может быть свободным текстом)
    service_title_snapshot VARCHAR(255) NOT NULL,  -- свободный текст клиента ИЛИ service.name
    service_price_snapshot NUMERIC(10,2),  -- NULL если нет service_id
    client_name_snapshot  VARCHAR(255) NOT NULL,  -- "Паша"/"я сам"/"сын 5 лет"
    start_at        TIMESTAMPTZ NOT NULL,  -- UTC
    end_at          TIMESTAMPTZ NOT NULL,  -- UTC
    status          VARCHAR(20) NOT NULL DEFAULT 'confirmed',  -- confirmed | cancelled | transferred
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (slot_id)  -- SQLite fallback: один booking на один slot
    CHECK (end_at > start_at)  -- invariant: duration > 0 (для free-text service duration default 60 мин)
);
CREATE INDEX idx_bookings_client ON bookings(client_id) WHERE status IN ('confirmed','transferred');
CREATE INDEX idx_bookings_master_start ON bookings(master_id, start_at);

-- 6.1 Postgres-only: EXCLUDE constraint для no-double-booking
-- Добавляется отдельной Alembic миграцией, только если dialect=postgresql
-- Требует: CREATE EXTENSION IF NOT EXISTS btree_gist;
-- ALTER TABLE bookings ADD CONSTRAINT no_overlap EXCLUDE USING gist (
--     master_id WITH =,
--     tstzrange(start_at, end_at) WITH &&
-- ) WHERE (status IN ('confirmed', 'transferred'));

-- 7. Лог уведомлений (idempotency)
CREATE TABLE notifications_log (
    id              BIGSERIAL PRIMARY KEY,
    booking_id       UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    kind            VARCHAR(30) NOT NULL CHECK (kind IN
                    ('remind_24h','remind_1h','master_new','master_cancel','master_transfer')),
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (booking_id, kind)
);
```

## First user journey (client side)

1. Клиент пишет `/start` → бот отвечает приветствием + inline-кнопкой "Записаться"
2. Клиент тапает "Записаться" → бот показывает inline-календарь (`aiogram_calendar`)
3. Клиент выбирает дату → бот показывает inline-кнопки с доступными часами (только `status='open'`)
4. Клиент тапает "14:00" → бот просит "На чьё имя записываем?" (Паша / я сам / сын 5 лет)
5. Клиент пишет имя → бот просит "Какая услуга?" (стрижка / окрашивание+стрижка / другое)
6. Клиент пишет услугу → бот показывает подтверждение:
   ```
   📅 17 марта 2026, 14:00
   💇‍♀️ Стрижка
   👤 Паша
   [✅ Подтвердить] [❌ Отмена]
   ```
7. Клиент тапает "✅ Подтвердить" → бот:
   - INSERT booking (slot.status = 'booked', slot_id UNIQUE guard)
   - Шедулит напоминания (24ч + 1ч)
   - Отправляет мастеру уведомление "Новая запись: Паша, стрижка, 17 марта 14:00"
   - Отвечает клиенту "✅ Вы записаны. Напомню за 24ч и за 1ч."
8. За 24ч до: клиенту → напоминание
9. За 1ч до: клиенту → напоминание

## First user journey (master side)

1. Екатерина пишет `/addslots 2026-03-17 11 12 13 14 15 16 17 18` → бот:
   - INSERT 8 rows в `slots` (status='open')
   - Отвечает "✅ Открыты слоты на 17 марта: 11, 12, 13, 14, 15, 16, 17, 18"
2. Екатерина пишет `/closeslot 2026-03-17 14` → бот:
   - UPDATE slot status='closed' WHERE master_id, slot_date, slot_hour
   - Если на слот уже есть booking → отказать ("на 14:00 уже записан Паша")
   - Отвечает "✅ Слот 14:00 закрыт"
3. Екатерина пишет `/today` → бот:
   - SELECT bookings JOIN slots WHERE slot_date=today AND status='confirmed'
   - Отвечает списком: "14:00 — Паша, стрижка / 16:00 — Татьяна, окрашивание"
4. Когда клиент записывается → Екатерине приходит уведомление
5. Когда клиент отменяет/переносит → Екатерине приходит уведомление

## FSM (aiogram StatesGroup)

```python
# bot/states.py
from aiogram.fsm.state import StatesGroup, State

class BookingStates(StatesGroup):
    # select_specialist = State()  # skip'ается в single-master (BB-001)
    selecting_date = State()
    selecting_slot = State()
    entering_name = State()
    entering_service = State()
    confirming = State()
```

## Структура проекта

```
barber-bot/
├── bot/
│   ├── __init__.py
│   ├── main.py              # entry: Bot, Dispatcher, scheduler.start(), dp.start_polling()
│   ├── config.py            # Settings (pydantic-settings): BOT_TOKEN, DATABASE_URL (async), DATABASE_URL_SYNC (sync для scheduler), ADMIN_ID, TIMEZONE
│   ├── db.py                # ASYNC engine + sessionmaker + Base (asyncpg/aiosqlite)
│   ├── scheduler.py         # SYNC engine (psycopg2) + AsyncIOScheduler + SQLAlchemyJobStore — ОТДЕЛЬНЫЙ от db.py
│   ├── models.py            # 7 таблиц (SQLAlchemy 2.0)
│   ├── states.py            # BookingStates FSM (BB-001)
│   ├── keyboards/
│   │   ├── __init__.py
│   │   ├── client.py        # inline: aiogram_calendar + slot picker + confirm
│   │   └── admin.py         # reply: /addslots /closeslot /today /week /services
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── client.py        # /start /book /mybookings /cancel
│   │   ├── admin.py         # /addslots /closeslot /today /week /services
│   │   └── common.py        # error handler, fallback
│   ├── services/
│   │   ├── __init__.py
│   │   ├── booking.py       # create_booking (SELECT FOR UPDATE на Postgres, UNIQUE guard на SQLite)
│   │   ├── slots.py         # add_slots, close_slot, get_available_slots
│   │   └── notifications.py # send_reminder (UNIQUE guard), schedule_for_booking, on_startup_scan
│   └── middlewares/
│       ├── __init__.py
│       └── role.py          # is_admin via DB (BB-009), inject current_business, current_master
├── scheduler.py             # SYNC engine (psycopg2) + AsyncIOScheduler + SQLAlchemyJobStore (BB-011) — отдельный файл от db.py
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 001_initial.py   # 7 таблиц БЕЗ EXCLUDE (работает на SQLite и Postgres)
│       └── 002_postgres_exclude.py  # EXCLUDE constraint + btree_gist ТОЛЬКО для Postgres (pre-deploy до первого запуска бота, на пустой БД)
├── tests/
│   ├── conftest.py          # fixtures: in-memory SQLite, mock Bot
│   ├── test_booking.py      # бизнес-логика без Telegram
│   ├── test_slots.py        # add_slots, close_slot, get_available_slots
│   ├── test_notifications.py # idempotency, UNIQUE guard, on_startup_scan
│   └── test_handlers.py    # FSM flow через aiogram testing utilities
├── .cursor/rules/
│   └── aiogram-anti-hallucination.mdc   # скопирован из library
├── spec.md                  # ЭТОТ файл — источник правды
├── AGENTS.md                # project-level правила для opencode
├── roadmap.md               # что отложено (Deferred features)
├── .env.example             # BOT_TOKEN=, DATABASE_URL=sqlite+aiosqlite:///./barber.db, ADMIN_ID=, TIMEZONE=Europe/Moscow
├── .gitignore               # .env, __pycache__, *.db, .pytest_cache, .venv, alembic/versions/__pycache__/
├── pyproject.toml          # aiogram>=3.x, sqlalchemy>=2.0, aiosqlite, asyncpg, apscheduler, alembic, pydantic-settings, psycopg2-binary (для SQLAlchemyJobStore в проде). [project.optional-dependencies].prod = [asyncpg, psycopg2-binary, alembic]
├── render.yaml              # (создаётся при деплое) web service + Postgres + pre-deploy alembic upgrade
├── README.md                # инструкция запуска (для разработчика)
└── USER_GUIDE.md            # инструкция для Екатерины (для пользователя, НЕ для разработчика)
```

## Константы и env

```python
# bot/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    BOT_TOKEN: str
    DATABASE_URL: str = "sqlite+aiosqlite:///./barber.db"  # ASYNC engine (ORM)
    DATABASE_URL_SYNC: str = ""  # SYNC engine для SQLAlchemyJobStore (psycopg2), пусто = MemoryJobStore в dev
    ADMIN_ID: int  # telegram_id Екатерины (в dev = её, в проде читается из businesses.telegram_owner_id)
    TIMEZONE: str = "Europe/Moscow"
    REMINDER_24H_BEFORE: int = 24  # часов до записи
    REMINDER_1H_BEFORE: int = 1
    CANCEL_MIN_HOURS: int = 24  # клиент может отменить/перенести за сколько часов до записи
    MISFIRE_GRACE_TIME: int = 3600  # секунд, Render free tier sleep 15 мин = 900 сек, ставим 3600 (1ч) запас
    SERVICE_DEFAULT_DURATION_MIN: int = 60  # если service_id NULL (free-text услуга), end_at = start_at + 60 мин

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
```

**Timezone invariant:** slot.slot_hour — ЛОКАЛЬНЫЙ час в business.timezone. При INSERT booking: `start_at = datetime.combine(slot_date, time(slot_hour), tzinfo=ZoneInfo(business.timezone)).astimezone(UTC)`. При отображении мастеру: `start_at.astimezone(ZoneInfo(business.timezone)).hour`.

**HTML escape:** `client_name_snapshot` sanitize через `html.escape()` в **service** `booking.py:create_booking` ПЕРЕД INSERT. НЕ в handler (иначе double-escape при /today render). DB хранит escaped, render в Telegram HTML parse mode БЕЗ повторного escape.

## Уведомления (BB-011 + BB-012 + BB-013)

> ⚠️ Дополнено после deep-analysis-critic 2026-07-18: misfire_grace_time, two-engine setup, fire_overdue_reminders, remove_job on cancel.

### Trigger'ы:
- **Новая запись** (`master_new`): при INSERT booking → мастеру "Новая запись: ..."
- **Отмена** (`master_cancel`): при UPDATE booking.status='cancelled' → мастеру "Отмена: ..." + **scheduler.remove_job для remind_24h + remind_1h** (cleanup, не полагаться только на UNIQUE guard)
- **Перенос** (`master_transfer`): при UPDATE booking.start_at/end_at (НЕ cancel+new, а реальный UPDATE той же строки) → мастеру "Перенос: ... → ..." + пересоздать scheduler jobs (remove старые + add новые)
- **24h напоминание** (`remind_24h`): APScheduler `add_job` на `start_at - 24h` → клиенту "Напоминаю: завтра в 14:00"
- **1h напоминание** (`remind_1h`): APScheduler `add_job` на `start_at - 1h` → клиенту "Через час: 14:00"

### Scheduler (two-engine setup, dev SQLite vs prod Postgres):

**КРИТИЧНО:** SQLAlchemyJobStore использует **sync** driver (pickle-сериализация). Async engine (asyncpg) НЕ подходит. Two-engine setup:

```python
# scheduler.py — отдельный от db.py файл, sync engine для jobstore
from sqlalchemy import create_engine
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from zoneinfo import ZoneInfo

# SYNC engine для SQLAlchemyJobStore (pickle не работает с asyncpg)
sync_engine = create_engine(
    "postgresql+psycopg2://...",  # НЕ asyncpg, НЕ aiosqlite
    pool_pre_ping=True,    # детектит мёртвые коннекты после Render sleep
    pool_recycle=1800,
    pool_size=5, max_overflow=10,
)

scheduler = AsyncIOScheduler(
    timezone=ZoneInfo("Europe/Moscow"),
    jobstores={"default": SQLAlchemyJobStore(engine=sync_engine)},
    job_defaults={
        "coalesce": True,           # сворачивает накопленные misfire в один запуск
        "misfire_grace_time": 3600,  # ⚠️ Render sleep 15 мин = 900 сек → 3600 сек (1ч) даёт запас
        "max_instances": 1,         # не запускать job параллельно
    },
)
```

**One-way door (pickle-сериализация):** Job kwargs сериализуются pickle. Рефактор `send_reminder(booking_id, kind)` → `send_reminder(booking_id, kind, locale)` ломает существующие unpickled jobs (TypeError). Миграция = drop all jobs + on_startup_scan пересоздаёт.

- **dev (SQLite)**: MemoryJobStore (нет pickle) + on-startup scan (BB-012) — пересоздаёт jobs каждый запуск
- **prod (Postgres)**: SQLAlchemyJobStore (BB-011) + on-startup scan (BB-012) для записей, созданных пока бот спал
- **Idempotency** (BB-013): `notifications_log` с UNIQUE(booking_id, kind) — catch UniqueViolationError → return

### on_startup flow (полный, не только upcoming):

```python
@dp.startup()
async def on_startup(bot: Bot):
    # 1. fire_overdue_reminders — записи которые пропустили напоминание пока бот спал
    #    (Render sleep 15 мин → misfire_grace_time=3600 сек покрывает, но если bot был down > 1ч — теряется)
    overdue = await db.fetch(
        "SELECT * FROM bookings "
        "WHERE status = 'confirmed' "
        "AND start_at > now() - interval '24 hours' AND start_at < now() "
        "AND NOT EXISTS (SELECT 1 FROM notifications_log WHERE booking_id=bookings.id AND kind='remind_24h')"
    )
    for b in overdue:
        await send_reminder(b.id, "remind_24h", bot=bot)  # bot последним для pickle-stability; UNIQUE guard inside send_reminder

    # 2. schedule_for_booking для upcoming
    upcoming = await db.fetch(
        "SELECT * FROM bookings WHERE start_at > now() "
        "AND start_at < now() + interval '25 hours' AND status = 'confirmed'"
    )
    for b in upcoming:
        await schedule_for_booking(b)  # add_job с replace_existing=True
    scheduler.start()
```

### Edge cases (из ресёрча BB-014 + critic Pass 2):
- Рестарт во время отправки → SQLAlchemyJobStore восстанавливает job
- Бот упал на 30 мин → `coalesce=True` сворачивает misfire в один запуск
- **Render free tier sleep 15 мин → `misfire_grace_time=3600` (1ч) покрывает**, плюс `pool_pre_ping=True` пересоздаёт dead connection к Postgres
- **Бот был down > 1ч → missed jobs теряются**, НО `on_startup → fire_overdue_reminders` сканирует bookings без notifications_log и отправляет пропущенные (UNIQUE guard отбивает дубли)
- 2 клиента одновременно тапают слот → UNIQUE(slot_id) на SQLite / EXCLUDE на Postgres отбивает дубль
- Бот упал после INSERT в notifications_log, до send_message → сообщение потеряно (MVP-допущение, прод — двухфазная схема: INSERT с `sent_at=NULL`, UPDATE `sent_at=now()` после send + reaper для зависших)
- **Отмена booking → `scheduler.remove_job(f"remind_24h_{booking_id}")` + `remove_job(f"remind_1h_{booking_id}")` явно** (не только UNIQUE guard)
- **Перенос booking → remove старые jobs + add_job с новым run_date + replace_existing=True**

## Безопасность

- Секреты в `.env` (в `.gitignore`), НЕ в коде
- `client_name_snapshot` — sanitize через `html.escape()` в **service** ПЕРЕД INSERT (защита от XSS в Telegram HTML parse mode), НЕ double-escape при /today render
- `/addslots`, `/closeslot`, `/today`, `/week`, `/services` — только для master (через middleware `role.py`, проверка `masters.telegram_id == message.from_user.id`)
- `/addslots` parsing валидация:
  - дата в формате `YYYY-MM-DD` (через `datetime.fromisoformat`, иначе "невалидная дата")
  - дата не в прошлом (`slot_date >= today`, иначе "нельзя создать слот в прошлом")
  - часы 0-23 (CHECK в БД + Python validation, иначе "час должен быть 0-23")
  - дубликаты часов (`11 11`) — IGNORE silently или warn? → warn "11 уже открыт"
  - если slot уже существует (UNIQUE(master_id, slot_date, slot_hour)) — skip с сообщением "уже открыт"
- `/cancel` для client:
  - валидация: `now < start_at - CANCEL_MIN_HOURS` (по умолчанию 24ч), иначе "нельзя отменить менее чем за 24ч"
  - в service: UPDATE booking.status='cancelled' → slot.status='open' → scheduler.remove_job для remind_24h + remind_1h → INSERT notifications_log(master_cancel) → send мастеру
- `/transfer` для client:
  - валидация: `now < start_at - CANCEL_MIN_HOURS` (24ч)
  - в service: UPDATE booking (slot_id, start_at, end_at) + UPDATE old slot.status='open' + UPDATE new slot.status='booked' + remove_job старые + add_job новые → INSERT notifications_log(master_transfer) → send мастеру
  - НЕ cancel+new (одна запись, master_transfer осмысленный)
 
### Prod (когда Екатерина скажет "хочу в прод")
- Render web service (free tier 90 дней, потом $7/мес)
- Render Postgres free (90 дней, потом $7/мес)
- `render.yaml`:
  ```yaml
  services:
    - type: web
      name: barber-bot
      env: python
      buildCommand: pip install -e .[prod]  # requirements.txt НЕ существует; pyproject.toml [project.optional-dependencies].prod
      startCommand: python -m bot.main
      envVars:
        - key: DATABASE_URL
          fromDatabase: { name: barber-db, property: connectionString }
        - key: BOT_TOKEN
          sync: false
        - key: ADMIN_ID
          sync: false
      preDeployCommand: alembic upgrade head
  databases:
    - name: barber-db
      plan: free
  ```
- Alembic миграция 002_postgres_exclude.py: `if dialect == 'postgresql': CREATE EXTENSION btree_gist; ALTER TABLE bookings ADD CONSTRAINT no_overlap EXCLUDE ...`

## Тесты

- `test_booking.py` — бизнес-логика (create_booking, cancel, transfer) без Telegram
- `test_slots.py` — add_slots, close_slot, get_available_slots
- `test_notifications.py` — idempotency, UNIQUE guard, on_startup_scan
- `test_handlers.py` — FSM flow (клиент пишет /start → ... → подтверждение)
- Coverage цель: ≥ 80% для `services/`
- Тесты БД: in-memory SQLite (`sqlite+aiosqlite:///:memory:`)
- Тесты handlers: `aiogram.tests` utilities (`MockedBot`, `MockedDispatcher`)

## Контрольно (когда MVP готов)

- [ ] Бот работает локально с SQLite
- [ ] `/start` → выбор даты → выбор слота → ввод имени → ввод услуги → подтверждение → мастеру уведомление
- [ ] `/addslots` → открыли слоты → `/closeslot` → закрыли
- [ ] `/today` / `/week` → список записей мастеру
- [ ] `/mybookings` → клиент может отменить (>24ч) или перенести (>24ч)
- [ ] Напоминание за 24ч + за 1ч уходят (проверить через `freezegun` в тестах)
- [ ] Уведомление мастеру при новой/отмене/переносе записи
- [ ] Тесты зелёные (`pytest -v`)
- [ ] README с инструкцией запуска
- [ ] USER_GUIDE.md с инструкцией для Екатерины
- [ ] Скринкаст demo для портфолио
- [ ] (deferred) Деплой на Render free tier

## Anti-паттерны (что НЕ делать — из ресёрча)

- ❌ God-file `main.py` со всей логикой — структура `bot/{handlers,services,keyboards,middlewares}/`
- ❌ `name`/`phone` в начале FSM — DON'T. Barber-bot порядок (single-master + плавающий график): **date → slot → name → service → confirm** (см. First user journey). Generic multi-master pattern из доноров (specialist → service → date → slot → confirm, +name в конце) — НЕ наш кейс, оставлен как референс для будущего multi-master.
- ❌ Reply-клавиатура с захардкоженными слотами — inline-кнопки только со свободными слотами
- ❌ `ADMIN_ID` в `.env` без таблицы `masters` — миграция на multi-master болезненна
- ❌ Бизнес-логика в handlers — вынести в `services/`
- ❌ MemoryJobStore на Render без on-startup scan — теряет jobs при cold start
- ❌ Materialized slots с авто-генерацией по шаблону — НЕ наш кейс (график плавающий)
- ❌ Aiogram 2.x imports (`from aiogram.dispatcher import Dispatcher` и т.п. — см. `.cursor/rules/aiogram-anti-hallucination.mdc`)

## Источники

- Ресёрч: `~/.config/opencode/references/donor-research/topics/booking-bot-architecture.md` (12 INSIGHT BB-001..014, 12 доноров)
- Уровень 2 PLANS: `~/.config/opencode/references/vibe-coding-learning-plan/PLANS.md` (Уроки 2.1-2.5)
- BEST-PRACTICES: `~/.config/opencode/references/vibe-coding-learning-plan/BEST-PRACTICES.md`
- Anti-hallucination rules: `.cursor/rules/aiogram-anti-hallucination.mdc` (из library vibe-coding-mentor)

---

## FSM edge cases

### `/cancel` внутри FSM (vs `/cancel` из `/mybookings`)
| Контекст | Что делает |
|---|---|
| FSM-idle (State(None)) | `/mybookings` → выбор брони → `UPDATE booking.status='cancelled'` (см. выше) |
| Внутри FSM (entering_name/entering_phone/choosing_service) | `await state.clear()` → сообщение "Ввод отменён. /book чтобы начать заново" |

Реализация: отдельный `@router.message(Command("cancel"), StateFilter("*"))` 
(ловит ВСЕ FSM state), ставится ДО `/cancel` из `/mybookings` в порядке 
обработчиков. StateFilter("*") = любой state, StateFilter(None) = idle.

---

### FSM таймаут (30 мин бездействия)

Проблема: FSM в aiogram не имеет встроенного TTL. Пользователь может 
вернуться через 3 часа — state всё ещё активен, но данные (слот, расписание) 
могли измениться.

Решение: **middleware с `last_message_at`** (вариант C).

```python
# bot/middlewares/session_timeout.py
SESSION_TTL_SEC = 1800  # 30 минут

class SessionTimeoutMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is None:
            return await handler(event, data)
        
        state_data = await state.get_data()
        last_at = state_data.get("last_message_at")
        if last_at:
            elapsed = (datetime.utcnow() - last_at).total_seconds()
            if elapsed > SESSION_TTL_SEC:
                await state.clear()
                return await event.answer(
                    "⏰ Сессия истекла (30 мин бездействия). "
                    "Начните заново через /book"
                )
        
        await state.update_data(last_message_at=datetime.utcnow())
        return await handler(event, data)
```

---

### FSM storage (переживает ли restart бота)
**Проблема:** В spec указано storage для **scheduler** (Memory vs SQLAlchemyJobStore), но не для **FSM state**. Дефолт aiogram `MemoryStorage` → теряется при restart. Barber-bot MVP на Render free tier — bot рестартит каждые 15 мин на free tier.

**Варианты:**
- **A) `MemoryStorage` (дефолт)** — теряется при restart. Если пользователь был посреди FSM → после restart `/book` начнёт с нуля (из-за middleware таймаута это уже так). НО: если Render restartнул bot посреди диалога, и пользователь через 5 сек пишет "Иван" — бот не поймет что это продолжение, будет ошибка.
- **B) `RedisStorage2`** — state переживает restart. Требует Redis. Render free tier не даёт Redis бесплатно.
- **C) `PostgresStorage`** — state в той же Postgres что и бизнес-данные. Переживает restart. Render free Postgres даёт 90 дней бесплатно. **Recommended** — единая инфраструктура, без новых зависимостей.

**Рекомендация:** **C** — PostgresStorage (prod) + MemoryStorage (dev). Dev = simple, prod = durable. Тестируется на in-memory SQLite (FSM state в памяти, бизнес-данные в SQLite).

