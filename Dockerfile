FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY bot ./bot
COPY scheduler.py ./

RUN pip install --no-cache-dir -e ".[prod]"

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && python -m bot.main"]
