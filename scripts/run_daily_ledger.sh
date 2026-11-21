#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 1. Wait up to 60s for the container to be running (e.g. after boot/wake)
for i in {1..12}; do
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "eve-trading-desk"; then
        break
    fi
    echo "[*] Waiting for eve-trading-desk container to become ready..."
    sleep 5
done

# 2. Wait up to 45s for external DNS/network connectivity
for i in {1..15}; do
    if docker compose exec -T eve-trading-desk python3 -c "import socket; socket.gethostbyname('login.eveonline.com')" >/dev/null 2>&1; then
        break
    fi
    echo "[*] Waiting for DNS / internet connection to initialize..."
    sleep 3
done

# 3. Create or update data/latest-sqlite.db (retry up to 4 times with byte-range resume)
MAX_SDE_ATTEMPTS=4
SDE_SYNCED=false

for attempt in $(seq 1 "$MAX_SDE_ATTEMPTS"); do
    if ./scripts/sync_sde.sh; then
        SDE_SYNCED=true
        break
    fi

    if [ "$attempt" -lt "$MAX_SDE_ATTEMPTS" ]; then
        WAIT_SEC=$((attempt * 10))
        echo "[!] SDE sync attempt ${attempt}/${MAX_SDE_ATTEMPTS} failed. Resuming in ${WAIT_SEC}s..."
        sleep "$WAIT_SEC"
    fi
done

if [ "$SDE_SYNCED" != "true" ]; then
    if [ -s "data/latest-sqlite.db" ]; then
        echo "[!] Warning: SDE update failed after ${MAX_SDE_ATTEMPTS} attempts; continuing with existing data/latest-sqlite.db."
    else
        echo "[!] Error: Initial EVE SDE download failed after ${MAX_SDE_ATTEMPTS} attempts and no local database exists. Aborting." >&2
        exit 1
    fi
fi

# 4. Delegate the full daily pipeline to workflows/daily_pipeline.py
docker compose exec -T eve-trading-desk python3 -m workflows.daily_pipeline
