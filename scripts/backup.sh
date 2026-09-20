#!/bin/bash
# Nightly pg_dump backup for barber-bot (Session 5.53).
# Cron on VPS: 30 3 * * * /opt/barber-bot/scripts/backup.sh
# Format: pg_dump -Fc (custom, compressed) -> verified via pg_restore --list
# Retention: 7 daily dumps (disk is 79% full — keep it lean, ~1-2 MB per dump).
set -euo pipefail

BACKUP_DIR=/opt/barber-bot/backups
RETENTION_DAYS=7
DB_CONTAINER=barber-bot-db-1
DB_USER=barber
DB_NAME=barber
STAMP=$(date +%Y-%m-%d_%H%M)

mkdir -p "$BACKUP_DIR"

DUMP="$BACKUP_DIR/barber_${STAMP}.dump"

# Dump straight from the container to the host (custom format = compressed).
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$DUMP"

# Verify: pg_restore --list must read the TOC (a corrupted dump fails here).
if ! docker exec -i "$DB_CONTAINER" pg_restore --list < "$DUMP" > /dev/null 2>&1; then
    echo "[$(date)] FATAL: dump $DUMP failed verification, keeping file for inspection" >&2
    exit 1
fi

SIZE=$(du -h "$DUMP" | cut -f1)
echo "[$(date)] OK: $DUMP ($SIZE, verified TOC)"

# Retention: drop dumps older than RETENTION_DAYS.
find "$BACKUP_DIR" -name 'barber_*.dump' -type f -mtime +"$RETENTION_DAYS" -delete
echo "[$(date)] retention: kept $(ls -1 "$BACKUP_DIR"/barber_*.dump 2>/dev/null | wc -l) dumps (>${RETENTION_DAYS}d removed)"
