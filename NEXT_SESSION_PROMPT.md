# NEXT_SESSION_PROMPT — barber-bot, handoff после 2026-09-06 (сессия 3)

## Текущее состояние (актуально)

**VPS:** `b67460b` (после handoff docs update, контейнер не пересобран — но код не менялся, VPS на `6f17f5e` работает).
**Origin/main:** `b67460b docs(handoff): update NEXT_SESSION_PROMPT — Variant D + guard B done, VPS on 6f17f5e`.
**Гейты:** ruff ✅ · mypy ✅ (25 src) · pytest 470 passed, 2 skipped.

Сделано в прошлых сессиях (2 → 3):
- ✅ `d7fe358` fix(openweek): bookings scoped to monday..sunday
- ✅ `8fa02e7` feat(week): show ALL future bookings (заголовок «Ближайшие записи:»)
- ✅ `fa9108b` feat(openweek): warn before overwriting existing days (guard B)
- ✅ `6f17f5e` feat(openweek): per-day window edit after apply (Variant D)
- ✅ `b67460b` docs(handoff): этот handoff update

## ⚠️ Две задачи следующей сессии (зафиксировано пользователем 2026-09-06)

### Задача 1 — UX: клиент не понимает как начать запись

**Симптом:** пользователь зашёл в бот, увидел пустое меню, не понял что нажать.
Пробовал случайные буквы — бот не реагировал (FSM пустой, fallback нет для текста вне FSM).
/cmd start и /book тоже не сработали в первый заход (TelegramConflictError из-за моего тестового
getUpdates ронял polling — починено, бот сейчас отвечает).

**Корень UX проблемы:** после подтверждения записи бот шлёт голый текст
«✅ Вы записаны. Напомню за 24ч и за 1ч.» **без inline-кнопок**. Клиент в тупике —
не знает как посмотреть свои записи, не знает как записаться ещё раз.

**Решение:**
- После `book_confirm_cb` success — inline-кнопки [📋 Мои записи] [💇 Ещё запись]
- На «Мои записи» — reuse `MyBookingsCancelCallbackData` keyboard (уже есть в `mybookings_keyboard`)
- На «Ещё запись» — симулировать `/book` (state.clear + тот же flow что cmd_book)
- Опционально: после `/start` тоже показать [📋 Мои записи] рядом с [💇 Записаться]
  (если у клиента уже есть записи — иначе скрыть)

**Где править:**
- `bot/handlers/client.py:1218` — после `await callback.message.answer("✅ Вы записаны...")`
  добавить `reply_markup=post_booking_keyboard(has_bookings=True)` (новая функция в keyboards)
- `bot/keyboards/client.py` — NEW `post_booking_keyboard()` builder
- `bot/handlers/client.py` — NEW handler для новой CallbackData `ClientMenuMyBookingsCallbackData`
  (или reuse `MyBookingsCancelCallbackData` если достаточно)
- `bot/handlers/start.py:46-49` — добавить кнопку [📋 Мои записи] в `client_inline_menu()`
  (проверять booking count через DB — опционально, можно всегда показывать, handler сам скажет «нет записей»)

**Тесты:**
- `test_book_confirm_renders_post_booking_keyboard` — после confirm есть inline-кнопки
- `test_post_booking_mybookings_button_starts_mybookings_flow` — тап [📋 Мои записи] → mybookings list
- `test_post_booking_again_button_starts_book_flow` — тап [💇 Ещё запись] → selecting_date state

---

### Задача 2 — Рефакторинг FSM: услуга ДО слота (главная задача)

**Симптом:** пользователь записался на стрижку и окрашивание на 16:00. Следующий
клиент должен видеть свободные слоты ИСХОДЯ ИЗ уже забронированных услуг (их длительности).
Сейчас бот показывает слоты по 30-мин grid, не учитывая duration реальных броней.

**Корень бага:** в `bot/services/slots.py:218-224` overlap считается с 30-мин ячейкой
(`slot.start + 30`), а не с duration услуги. Бронь 16:00-18:00 (Окрашивание 120 мин)
не блокирует слот 15:30, потому что 15:30-16:00 не пересекает 16:00-18:00. А клиент
выбирает 15:30 + Стрижка (60 мин) → реальный booking 15:30-16:30 пересекает → падает
на confirm.

**Flow сейчас:** дата → слот → имя → услуга → confirm
**Flow надо:** дата → услуга → слот → имя → confirm

**Изменения:**

**`bot/handlers/client.py`** — reorder FSM flow:
1. `book_date_cb` / `simple_calendar_cb` / `book_date_cb` — после даты → `entering_service`
   (вместо `selecting_slot`)
2. NEW handler `service_picker_cb` already exists на `entering_service` — после выбора
   услуги → `selecting_slot` + фетч слотов с `service.duration_minutes` (вместо
   `SERVICE_DEFAULT_DURATION_MIN`)
3. `service_msg` (free-text услуга) — после → `selecting_slot` с
   `SERVICE_DEFAULT_DURATION_MIN` (60 мин, нет точной duration)
4. `name_msg` — после имени → `confirming` (вместо `entering_service`)
5. `slot_30_cb` / `slot_cb` — НЕ трогаем (работают как сейчас, только state order другой)

**`bot/services/slots.py:218-224`** — фикс overlap:
```python
# Было:
slot_end_utc = slot.start_at_utc + timedelta(minutes=30)
# Станет:
slot_end_utc = slot.start_at_utc + timedelta(minutes=min_duration_min)
```
`min_duration_min` теперь передаётся = `service.duration_minutes` (а не `SERVICE_DEFAULT_DURATION_MIN`).

**`bot/handlers/client.py:400, 444`** — передавать `service.duration_minutes` в
`get_available_slots_30(min_duration_min=...)` (нужно поднять duration из FSM state
после выбора услуги).

**Что НЕ меняем:**
- `BookSlot30CallbackData`, `BookConfirmCallbackData` — форматы те же
- `BookingStates` names — те же (`entering_service`, `selecting_slot`, `entering_name`, `confirming`)
- Transfer flow (`TransferStates`) — не трогаем, там услуга уже известна из брони
- `create_booking` в `bot/services/booking.py` — уже работает с любой duration

**Edge cases:**
- Услугу архивнули между выбором и confirm → `book_confirm_cb` уже ловит через re-SELECT
- "Своя услуга" free-text → duration = `SERVICE_DEFAULT_DURATION_MIN` (60) для фильтра
- Если services table пустой → `/book` показывает «У мастера нет услуг. Напишите услугу текстом»
- Если услуга выбрана, но слотов нет → «На эту дату нет окна под {duration}. Выберите другую дату»

**Тесты (ожидаемо сломаются — FSM order):**
- `test_admin_handlers.py` — ~10-15 тестов на booking flow (date→slot→name→service→confirm)
  нужно переставить шаги (date→service→slot→name→confirm)
- NEW: `test_book_flow_service_before_slot` — выбор услуги ДО слота
- NEW: `test_slots_filtered_by_service_duration` — слот 15:30 скрыт при брони 16:00-18:00
  (duration 120, slot 15:30+120 = 17:30 пересекает 16:00-18:00)
- NEW: `test_slots_shown_for_short_service` — слот 15:30 доступен для Стрижки (60) если
  бронь 16:00-18:00 (15:30+60 = 16:00, half-open overlap = OK)

**Гейты:**
1. `deep-analysis-protocol` Pass 1-4 — risk-class = materially changes (FSM reorder,
   нет миграции БД, нет contract/security). Skip для trivial, это не trivial.
2. `qa-verify-and-fix` — ruff + mypy + pytest (целевое 480+ passed)
3. `qa-code-review` — logic change в FSM + overlap fix, code-reviewer subagent
4. Pre-push: skip (pet-project git free per AGENTS.md § git-repo-categories)

**Dry-run перед кодом (обязательно):**
- Показать пользователю план (этот handoff уже план) → получить согласие
- Реализовать по шагам: slots.py overlap fix → client.py FSM reorder → tests → keyboards
- Гейты после

**Сложность:** medium-high. ~10-15 правок в handlers, 1 правка в services,
~15-20 правок в тестах. Время ~1-2 часа работы ассистента.

---

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
  НЕ только `restart` (restart не пересобирает образ — incident с прошлой сессией,
  когда `get_bookings_for_date_range` отсутствовал в контейнере после restart).
- Если `git pull` падает на divergent branches:
  ```bash
  sshpass -p "$BARBER_PASS" ssh root@VPS_HOST_FROM_CRED_FILE \
    "cd /opt/barber-bot && git reset --hard origin/main && docker compose up -d --build bot"
  ```
- Логи: `docker logs barber-bot-bot-1 --since 30m 2>&1 | tail -100`
- Время VPS: UTC (логи в UTC, MSK = UTC+3)

## Pet-project git

`git add -A && git commit -m "..." && git push` — pet-project git free
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
- 06.09 16:00-17:00 Стрижка (но workday 19-20 — бронь вне workday window, как прошла?)
- 10.09 12:30-14:30 Окрашивание и стрижка

⚠️ Аномалия: бронь 06.09 16:00-17:00 создана 08:31 UTC, а workday 06.09 открыт
19:00-20:00. Это значит бронь создалась ДО того, как workday сузили до 19-20 (было
10:30-19:30 в исходной неделе). Сейчас `create_booking` валидирует [start_at, end_at] ∈
[workday.start, workday.end] — но это валидация на момент создания, а не ретроактивная.
Если workday сузили после брони — `update_workday` должен был дать WorkDayShrinkError
(бронь вне нового окна). Как прошла — отдельный баг, не блокер для задач 1-2.

## Первое действие в новой сессии

1. Прочитать этот промт (уже прочитан)
2. VPS на `6f17f5e` — деплой НЕ требуется (если только не было новой правки).
3. Smoke-test в Telegram: проверить что /start показывает inline-кнопку [💇 Записаться],
   после confirm бот шлёт «✅ Вы записаны» (без кнопок — это и есть проблема задача 1).
4. Начать с **Задачи 1** (UX кнопок после confirm) — проще, ~30-45 мин, сразу видимый результат.
5. После гейтов Задачи 1 → коммит + деплой + smoke-test.
6. Затем **Задача 2** (FSM reorder) — dry-run по этому handoff, согласие пользователя,
   реализация по шагам (slots.py fix → client.py reorder → tests → keyboards), гейты,
   code review, коммит, деплой, smoke-test.

## Промпт для вставки в начало следующей сессии

```
Продолжим barber-bot. Прочитай NEXT_SESSION_PROMPT.md (там handoff + 2 задачи).
Две задачи:
1. UX — после confirm добавить inline-кнопки [📋 Мои записи] [💇 Ещё запись],
   чтобы клиент не зависал. Начни с этой (быстрее).
2. FSM reorder — услуга ДО слота (главная). Сейчас overlap 30-мин grid, не duration.
   Бронь 16:00-18:00 (Окрашивание 120) не блокирует слот 15:30 → падает на confirm
   если клиент выбрал Стрижку (60). Фикс: услуга сначала → duration известен →
   слоты фильтруются по реальной длительности.

VPS на 6f17f5e, деплой не нужен пока. Гейты: ruff + mypy + pytest.
Pet-project git free. Deep-analysis-protocol перед задачей 2 (materially changes,
FSM reorder). qa-code-review после.
```
