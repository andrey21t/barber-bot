#!/bin/bash
# Offsite backup: pull latest dumps from VPS to Mac (Session 5.54).
# Run: manually or via launchd daily (com.barber.offsite-backup.plist).
# Credentials: ~/.config/opencode/references/barber-bot-deploy-credentials.md
# (NOT committed to git — AGENTS.md § git-repo-categories).

set -euo pipefail

CRED_FILE="$HOME/.config/opencode/references/barber-bot-deploy-credentials.md"
VPS_USER="root"
REMOTE_DIR="/opt/barber-bot/backups"
LOCAL_DIR="$HOME/barber-bot-backups"
RETENTION_DAYS=7

if [[ ! -f "$CRED_FILE" ]]; then
    echo "[$(date)] FATAL: cred file not found: $CRED_FILE" >&2
    exit 1
fi

VPS_HOST=$(grep -E '^HOST:' "$CRED_FILE" | sed 's/^HOST: //')
VPS_PASS=$(grep -E '^PASS:' "$CRED_FILE" | sed 's/^PASS: //')
if [[ -z "$VPS_PASS" || -z "$VPS_HOST" ]]; then
    echo "[$(date)] FATAL: no HOST/PASS in $CRED_FILE" >&2
    exit 1
fi

mkdir -p "$LOCAL_DIR"

STAMP=$(date +%Y-%m-%d_%H%M)

# Pull all dumps that are newer than what we have locally.
# rsync would be cleaner but needs SSH key; scp + timestamp check works.
REMOTE_FILES=$(SSHPASS="$VPS_PASS" sshpass -e ssh \
    -o StrictHostKeyChecking=no \
    -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no \
    "$VPS_USER@$VPS_HOST" \
    "ls -1 $REMOTE_DIR/barber_*.dump 2>/dev/null" || true)

if [[ -z "$REMOTE_FILES" ]]; then
    echo "[$(date)] WARN: no dumps on VPS yet (first cron run pending?)" >&2
    exit 0
fi

COPIED=0
for REMOTE in $REMOTE_FILES; do
    BASENAME=$(basename "$REMOTE")
    LOCAL="$LOCAL_DIR/$BASENAME"
    if [[ -f "$LOCAL" ]]; then
        continue  # already have this one
    fi
    SSHPASS="$VPS_PASS" sshpass -e scp \
        -o StrictHostKeyChecking=no \
        -o PreferredAuthentications=password \
        -o PubkeyAuthentication=no \
        "$VPS_USER@$VPS_HOST:$REMOTE" "$LOCAL"
    SIZE=$(du -h "$LOCAL" | cut -f1)
    echo "[$(date)] OK: pulled $BASENAME ($SIZE)"
    COPIED=$((COPIED + 1))
done

if [[ "$COPIED" -eq 0 ]]; then
    echo "[$(date)] all dumps already up-to-date locally"
else
    echo "[$(date)] pulled $COPIED new dump(s)"
fi

# Retention: drop local dumps older than RETENTION_DAYS.
find "$LOCAL_DIR" -name 'barber_*.dump' -type f -mtime +"$RETENTION_DAYS" -delete
REMAINING=$(ls -1 "$LOCAL_DIR"/barber_*.dump 2>/dev/null | wc -l)
echo "[$(date)] local retention: $REMAINING dump(s) kept (>${RETENTION_DAYS}d removed)"
