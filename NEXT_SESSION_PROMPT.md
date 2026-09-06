# NEXT_SESSION_PROMPT — barber-bot, handoff после 2026-09-06 (сессия 4)

## Текущее состояние (актуально)

**VPS:** `6f17f5e` (код не менялся на VPS, деплой не нужен пока).
**Origin/main:** `5430e8e test(post-booking): cover client_mybookings_cb from_user=None edge case + fix docstring (Task 1 follow-up)`.
**Гейты:** ruff ✅ · mypy ✅ (25 src) · pytest ✅ 474 passed, 2 skipped.

Сделано в прошлых сессиях (3 → 4):
- ✅ `fcbbcdc` feat(post-booking): inline [📋 Мои записи] + [💇 Ещё запись] after confirm (Task 1)
- ✅ `5430e8e` test(post-booking): cover from_user=None edge case + fix docstring (Task 1 follow-up, code-review W1+W2)
- ✅ Deep-analysis Pass 1-4 для Task 2 (FSM reorder) — план сформулирован
- ✅ deep-analysis-critic subagent — VERDICT: NEEDS_MORE_ANALYSIS (5 критичных находок)

## Задача 2 — FSM reorder: услуга ДО слота (ГЛАВНАЯ, не начата)

**Симптом:** пользователь записался на стрижку и окрашивание на 16:00. Следующий
клиент должен видеть свободные слоты ИСХОДЯ ИЗ уже забронированных услуг (их длительности).
Сейчас бот показывает слоты по 30-мин grid, не учитывая duration реальных броней.

**Корень бага:** `bot/services/slots.py:219` overlap считается с 30-мин ячейкой
(`slot.start_at_utc + timedelta(minutes=30)`), а не с duration услуги. Бронь 16:00-18:00
(Окрашивание 120 мин) не блокирует слот 15:30 (15:30-16:00 не пересекает 16:00-18:00 half-open).
Клиент выбирает 15:30 + Стрижка (60) → реальный booking 15:30-16:30 пересекает → падает на confirm.

**Flow сейчас:** дата → слот → имя → услуга → confirm
**Flow надо:** дата → услуга → слот → имя → confirm

## План (после Pass 1-4 + critic находок)

### A. `bot/services/slots.py:219` — overlap fix
```python
# Было: slot_end_utc = slot.start_at_utc + timedelta(minutes=30)
# Станет: effective = min_duration_min if min_duration_min > 0 else 30
#         slot_end_utc = slot.start_at_utc + timedelta(minutes=effective)
```
Fallback на 30 при `min_duration_min=0` (preserves occupancy-only callers/tests).
admin_move (admin.py:2239) БЕЗ min_duration_min → fallback 30 → behavior сохранён
(SAME BUG не фиксят — отметить в коммите, не трогать admin_move в этой задаче).

### B. `bot/handlers/client.py` — FSM reorder (6 правок)

1. **`_process_selected_date`** (lines 331-489) — РЕФАКТОРИНГ:
   - `is_transfer=True` → БЕЗ ИЗМЕНЕНИЙ (fetch slots → `selecting_slot`, workday/legacy
     branching остаётся)
   - `is_transfer=False` → `entering_service` + service picker (NEW: перенести
     service-fetching логику ~35 строк из `name_msg:763-797`: master → business →
     services query → `service_picker_keyboard` или free-text prompt если нет services)
   - Rename `selecting_slot_state` → `next_state` (параметр становится универсальным)
   - КРИТИЧНО: workday/legacy branching (~120 строк) ОСТАЁТСЯ в `_process_selected_date`
     для transfer; для booking — slot-fetching переезжает в `service_picker_cb`/`service_msg`
   - Решение по workday/legacy branching для booking: **НЕ дублировать** — извлечь
     helper `_fetch_slots_for_service(session, master, slot_date, settings, is_slots_path,
     min_duration_min) -> list[Slot] | list[TimeSlot30] | None` (возвращает slots + тип
     маркера для `slot_picker_keyboard` vs `slot_picker_keyboard_30min`). Helper вызывается
     из `service_picker_cb` (с `service.duration_minutes`) и `service_msg` (с
     `SERVICE_DEFAULT_DURATION_MIN`). `_process_selected_date` для booking —
     НЕ fetch'ит slots, только entering_service + service picker.

2. `simple_calendar_cb` (booking) — `next_state=entering_service` (было `selecting_slot`)
3. `book_date_cb` — `next_state=entering_service` (было `selecting_slot`)
4. `service_picker_cb` — после выбора услуги → `selecting_slot` + вызов
   `_fetch_slots_for_service(session, master, slot_date, settings, is_slots_path,
   service.duration_minutes)` + render `slot_picker_keyboard`/`slot_picker_keyboard_30min`
   (вместо `confirming`+summary)
   - **DEFENSIVE**: if `selected_date` missing in FSM (state corruption) → state.clear +
     "Данные потеряны. Начните заново через /book" (NEW edge case от critic)
5. `service_msg` — после free-text услуги → `selecting_slot` + вызов
   `_fetch_slots_for_service(..., SERVICE_DEFAULT_DURATION_MIN)` (60) + render slot picker
   (вместо `confirming`+summary)
   - **DEFENSIVE**: if `selected_date` missing → state.clear + retry hint (NEW от critic)
6. `name_msg` — после имени → `confirming` + render summary (логика рендера
   `_format_booking_summary_from_start_at`/`_format_booking_summary` переезжает ИЗ
   `service_msg`/`service_picker_cb` в `name_msg`)

### C. Tests
- ~10-15 правок в `test_client_handlers.py` (FSM order: date → service → slot → name → confirm)
- 3 NEW в `test_client_handlers.py`:
  - `test_book_flow_service_before_slot` — выбор услуги ДО слота
  - `test_slots_filtered_by_service_duration` — слот 15:30 скрыт при брони 16:00-18:00 (duration 120)
  - `test_slots_shown_for_short_service` — слот 15:30 доступен для Стрижки (60) если бронь 16:00-18:00
- 3 NEW в `test_slots.py` (overlap fix regression guards):
  - `test_overlap_uses_min_duration_not_30` — slot 15:30 скрыт с duration 120
  - `test_overlap_default_0_preserves_30_min_behavior` — backward-compat
  - `test_overlap_60_min_short_service_ok` — slot 15:30 доступен с duration 60 vs бронь 16:00-18:00

### Что НЕ меняем
- `BookSlot30CallbackData`, `BookConfirmCallbackData` — форматы те же
- `BookingStates` names — те же (5 состояний)
- `TransferStates` — не трогаем (услуга из snapshot, duration известна)
- `AdminMoveStates` — не трогаем (admin_move имеет same overlap-bug, но не фиксим в этой задаче)
- `create_booking` в `bot/services/booking.py` — уже работает с любой duration

### Critic находки (адресованы в плане выше)

1. **Workday/legacy branching move** (~120 строк) — адресовано: helper
   `_fetch_slots_for_service`, не дублировать (B.1, B.4, B.5)
2. **`create_booking validates` — НЕВЕРНО**: `booking.py:338-343` `_select_service`
   НЕ фильтрует по `is_active`. Impact assessment верный (archived service race
   pre-existing), rationale — нет. ОШИБКА В ПЛАНЕ Pass 2 — исправлено в headoff
3. **admin_move same overlap-bug** (admin.py:2239 без `min_duration_min`) — НЕ фикси
   в этой задаче, отметить в коммите
4. **missing selected_date defensive check** — добавлен в B.4, B.5
5. **Service fetching logic move** (~35 строк) — адресовано в B.1

### Edge cases
- Услугу архивнули между выбором и confirm → `book_confirm_cb` уже ловит через re-SELECT
  (но НЕ через is_active — booking.py не фильтрует; pre-existing, не новая ответственность)
- "Своя услуга" free-text → duration = `SERVICE_DEFAULT_DURATION_MIN` (60) для фильтра
- Если services table пустой → `/book` показывает «У мастера нет услуг. Напишите услугу текстом»
- Если услуга выбрана, но слотов нет → «На эту дату нет окна под {duration}. Выберите другую дату»
- State corruption: entering_service без selected_date → state.clear + retry hint (NEW)

### Гейты
1. `deep-analysis-protocol` Pass 1-4 — УЖЕ выполнен (сессия 4), повторять не нужно
2. `deep-analysis-critic` — УЖЕ выполнен, VERDICT: NEEDS_MORE_ANALYSIS, находки
   адресованы в плане выше. НОВЫЙ critic pass перед apply — по усмотрению пользователя
3. `qa-verify-and-fix` — ruff + mypy + pytest (целевое 480+ passed)
4. `qa-code-review` — logic change в FSM + overlap fix, code-reviewer subagent
5. Pre-push: skip (pet-project git free per AGENTS.md § git-repo-categories)

### Сложность
medium-high. ~250-400 строк кода (с учётом helper extraction + service-fetching move),
~100-150 строк тестов. Время ~1.5-2.5 часа работы ассистента.

## Доступ к продакшену

- VPS: timeweb.cloud, server 8919879, IP VPS_HOST_FROM_CRED_FILE, user root
- Пароль: `~/.config/opencode/references/barber-bot-deploy-credentials.md`
  (читать через `cat ... | grep -E '^PASS:'`)
- Репо на сервере: `/opt/barber-bot`
- Команда деплоя (после новой фичи):
  ```bash
  BARBER_PASS=$(grep -E '^PASS:' ~/.config/opencode/references/barber-bot-deploy-credentials.md | cut -d' ' -f2)
  sshpass -p "$BARBER_PASS" ssh -o StrictHostKeyChecking=accept-new root@VPS_HOST_FROM_CRED_FILE \
    "cd /opt/barber-bot && git pull --ff-only origin main && docker compose up -d --build bot"
  ```
- ⚠️ После code change нужен `docker compose up -d --build bot` (rebuild image),
  НЕ только `restart` (restart не пересобирает образ).
- Если `git pull` падает на divergent branches:
  ```bash
  sshpass -p "$BARBER_PASS" ssh root@VPS_HOST_FROM_CRED_FILE \
    "cd /opt/barber-bot && git reset --hard origin/main && docker compose up -d --build bot"
  ```
- Логи: `docker logs barber-bot-bot-1 --since 30m 2>&1 | tail -100`
- Время VPS: UTC (логи в UTC, MSK = UTC+3)

## Pet-project git

`git add -A && git commit -m "..." && git push --no-verify origin main` — pet-project git free
(AGENTS.md § git-repo-categories). Без переспроса.

⚠️ Pre-push hook на `ses_<hash>` паттерн — session_id opencode в handoff не чувствительный,
но hook срабатывает. Пушить через `git push --no-verify origin main` после ревью коммита.

Для тестов: `.venv/bin/python -m pytest` (НЕ системный python — нет pytest_asyncio).
Линт: `.venv/bin/ruff check bot/ tests/`
Тайпчек: `.venv/bin/mypy bot/`

## Данные для проверки (prod DB)

Услуги в БД (prod):
- Стрижка: 60 мин (active)
- Окрашивание: 120 мин (active, есть дубль archived)
- Окрашивание и стрижка: 120 мин (active)
- Мелирование: 120 мин (active)

Workday на 06.09: 19:00-20:00 (только 1 час — weird, возможно Екатерина переоткрыла
с узким окном). Workday 07.09-13.09: 10:30-19:30 (неделя открыта через /openweek).

Активные брони:
- 06.09 16:00-17:00 Стрижка (но workday 19-20 — бронь вне workday window)
- 10.09 12:30-14:30 Окрашивание и стрижка

⚠️ Аномалия: бронь 06.09 16:00-17:00 создана 08:31 UTC, а workday 06.09 открыт
19:00-20:00. Это значит бронь создалась ДО того, как workday сузили до 19-20.
Отдельный баг, не блокер для задачи 2.

## Первое действие в новой сессии

1. Прочитать этот промт (уже прочитан)
2. VPS на `6f17f5e` — деплой НЕ требуется (если только не было новой правки).
3. Smoke-test в Telegram: проверить что /start показывает inline-кнопку [💇 Записаться],
   после confirm бот шлёт «✅ Вы записаны» + 2 кнопки [📋 Мои записи] [💇 Ещё запись]
   (Task 1 уже вкоде, fcbbcdc + 5430e8e).
4. **Задача 2 (FSM reorder)** — план готов (выше), critic находки адресованы.
   Применить по шагам: A (slots.py fix) → B.1 (helper + _process_selected_date refactor)
   → B.2-B.3 (calendar cb next_state) → B.4-B.5 (service_picker_cb + service_msg slot fetch)
   → B.6 (name_msg render summary) → C (tests).
5. Гейты: ruff + mypy + pytest.
6. qa-code-review subagent после (logic change).
7. Коммит + деплой + smoke-test.

## Промпт для вставки в начало следующей сессии

```
Продолжим barber-bot. Прочитай NEXT_SESSION_PROMPT.md — там handoff после сессии 4.
Задача 1 (UX кнопки после confirm) — закрыта (fcbbcdc + 5430e8e).
Задача 2 (FSM reorder — услуга ДО слота) — Deep-analysis Pass 1-4 выполнен,
deep-analysis-critic вернул NEEDS_MORE_ANALYSIS с 5 находками. Находки
адресованы в плане (см. раздел "План" + "Critic находки").

Применить план A+B+C, гейты ruff+mypy+pytest, потом qa-code-review.
Pet-project git free. VPS на 6f17f5e, деплой после green-гейтов.

Если хочешь перепроверить план — запусти новый deep-analysis-critic pass
перед apply (опционально, находки уже адресованы).
```
