#!/usr/bin/env bash
set -e

# Change to the project directory
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Wait up to 60 seconds if Docker daemon is still starting on boot
for i in {1..12}; do
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "eve-trading-desk"; then
        break
    fi
    echo "[*] Waiting for eve-trading-desk container to become ready..."
    sleep 5
done

echo "[*] Running 50B Multi-Desk Waterfall Allocation..."
docker compose exec -T eve-trading-desk python3 scripts/generate_ledgers.py --desk all

echo "[*] Refreshing live order book telemetry..."
docker compose exec -T eve-trading-desk curl -s -X POST http://localhost:8000/api/market/refresh-prices > /dev/null 2>&1 || true

echo "[+] Daily ledger run completed successfully at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
