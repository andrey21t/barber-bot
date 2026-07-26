# barber-bot

Telegram-бот для записи к парикмахеру. Skeleton (без бизнес-логики).

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # заполнить BOT_TOKEN и ADMIN_ID
```

## Запуск

```bash
python -m bot.main
```

## Тесты

```bash
pytest -v
```

## Линт / типы

```bash
ruff check .
mypy bot
```
