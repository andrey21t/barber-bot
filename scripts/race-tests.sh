#!/usr/bin/env bash
# Race-тесты на throwaway Postgres (pg_advisory_xact_lock семантика).
#
# Запускает ТОЛЬКО `-k concurrent_race_postgres` в tests/test_multi_client.py:
# под POSTGRES_RACE_TESTS=1 тесты, создающие engine из Settings (bot.db,
# scheduler), видели бы Postgres. Scope ограничен сознательно.
#
# known-limit: prod-ветка SQLAlchemyJobStore (scheduler.py:65-73) in-process
# не проверяется — minimal-deps решение, этот скрипт про advisory-lock гонки.
#
# Usage: ./scripts/race-tests.sh

set -euo pipefail

CONTAINER="barber-race-pg"
PORT="55432"
URL="postgresql+asyncpg://barber:barber@localhost:${PORT}/barber"

# --- Precheck 1: docker вообще жив? ---
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker не установлен. Установи Docker Desktop."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker не запущен. Запускаю Docker Desktop…"
  open -a Docker 2>/dev/null || { echo "ERROR: Docker Desktop не найден. Запусти вручную и повтори."; exit 1; }
  echo "Ждём docker engine…"
  for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
  docker info >/dev/null 2>&1 || { echo "ERROR: docker engine не поднялся за 60с."; exit 1; }
fi

# --- Cleanup от предыдущего прогона (идемпотентность) ---
docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true

# --- Запуск контейнера ---
echo "Запускаю postgres:16 на :${PORT}…"
docker run --rm -d --name "${CONTAINER}" -p "${PORT}:5432" \
  -e POSTGRES_PASSWORD=barber -e POSTGRES_USER=barber -e POSTGRES_DB=barber \
  postgres:16 >/dev/null

cleanup() {
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- Readiness-wait (pg_isready, до 10с): без него первый прогон flaky-красный ---
echo "Ждём готовности Postgres…"
for _ in $(seq 1 20); do
  docker exec "${CONTAINER}" pg_isready -U barber -q && break
  sleep 0.5
done
docker exec "${CONTAINER}" pg_isready -U barber -q || { echo "ERROR: Postgres не поднялся за 10с."; exit 1; }

# --- Прогон race-семейства ---
echo ""
echo "=== pytest: concurrent_race_postgres (Postgres) ==="
POSTGRES_RACE_TESTS=1 DATABASE_URL="${URL}" \
  .venv/bin/python -m pytest tests/test_multi_client.py -k concurrent_race_postgres -v

echo ""
echo "OK: race-тесты зелёные. Контейнер удалён trap'ом."
