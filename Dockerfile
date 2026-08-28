FROM python:3.12-slim

WORKDIR /app

# ru_RU.UTF-8 для aiogram_calendar.SimpleCalendar(locale="ru_RU").
# Без locale-gen SimpleCalendar падает с locale.Error: unsupported locale setting
# (python:3.12-slim не содержит ru_RU по умолчанию).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev locales && \
    sed -i '/ru_RU.UTF-8/s/^# //g' /etc/locale.gen && \
    locale-gen && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY bot ./bot
COPY scheduler.py ./

RUN pip install --no-cache-dir -e ".[prod]"

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && python -m bot.main"]
