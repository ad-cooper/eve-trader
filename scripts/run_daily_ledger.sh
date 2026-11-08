#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 1. Wait up to 60 seconds if Docker daemon is still initializing
for i in {1..12}; do
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "eve-trading-desk"; then
        break
    fi
    echo "[*] Waiting for eve-trading-desk container to become ready..."
    sleep 5
done

# 2. Wait up to 45 seconds for network / DNS to be active after wake
for i in {1..15}; do
    if docker compose exec -T eve-trading-desk python3 -c "import socket; socket.gethostbyname('login.eveonline.com')" >/dev/null 2>&1; then
        break
    fi
    echo "[*] Waiting for DNS / internet connection to initialize..."
    sleep 3
done

# 3. Synchronize daily Forge market history archive into DuckDB
echo "[*] Syncing latest daily market history archives into DuckDB..."
docker compose exec -T eve-trading-desk python3 scripts/sync_market_data.py || echo "[!] Market archive sync encountered an issue, proceeding with cached DuckDB history..."

# 4. Generate dynamic multi-desk ledgers
echo "[*] Running Dynamic Multi-Desk Waterfall Allocation (Auto-Equity)..."
docker compose exec -T eve-trading-desk python3 scripts/generate_ledgers.py --desk all --capital auto

# 5. Refresh live order book telemetry
echo "[*] Refreshing live order book telemetry..."
docker compose exec -T eve-trading-desk curl -s -X POST http://localhost:8000/api/market/refresh-prices > /dev/null 2>&1 || true

echo "[+] Daily ledger run completed successfully at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
