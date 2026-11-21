#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

BASE_URL="https://www.fuzzwork.co.uk/dump"
FILE_NAME="latest-sqlite.db.gz"
DB_PATH="data/latest-sqlite.db"
MD5_CACHE="data/latest-sqlite.db.md5"
FAIL_STAMP="data/.sde_last_fail"
COOLDOWN_SECONDS=900
MAX_CHUNK_RETRIES=10
MODE="${1:-}"

mkdir -p data
rm -f "${DB_PATH}.gz.tmp" "${DB_PATH}.tmp"

calc_md5() {
    if command -v md5 >/dev/null 2>&1; then
        md5 -q "$1"
    else
        md5sum "$1" | awk '{print $1}'
    fi
}

# 1. Poll the lightweight .md5sum endpoint
REMOTE_MD5="$(curl -fsSL --retry 3 --retry-delay 2 "${BASE_URL}/${FILE_NAME}.md5sum" | awk '{print $1}' | tr -d '[:space:]' || true)"

if [ -z "$REMOTE_MD5" ]; then
    if [ -s "$DB_PATH" ]; then
        echo "[!] Could not reach Fuzzwork .md5sum endpoint; keeping existing $DB_PATH."
        exit 0
    else
        echo "[!] Failed to fetch SDE checksum and no local $DB_PATH exists." >&2
        exit 1
    fi
fi

LOCAL_MD5=""
if [ -f "$MD5_CACHE" ]; then
    LOCAL_MD5="$(tr -d '[:space:]' < "$MD5_CACHE")"
fi

# 2. Skip download if DB exists and MD5 matches
if [ -s "$DB_PATH" ] && [ "$REMOTE_MD5" = "$LOCAL_MD5" ]; then
    rm -f "$FAIL_STAMP"
    echo "[=] EVE SDE is up to date (MD5: ${REMOTE_MD5})."
    exit 0
fi

# 3. Enforce cooldown on automated polls after a recent failed attempt
if [ "$MODE" = "--import-on-change" ] && [ -f "$FAIL_STAMP" ]; then
    LAST_FAIL="$(cat "$FAIL_STAMP" 2>/dev/null || echo 0)"
    NOW="$(date +%s)"
    ELAPSED=$((NOW - LAST_FAIL))
    if [ "$ELAPSED" -lt "$COOLDOWN_SECONDS" ]; then
        REMAINING=$(( (COOLDOWN_SECONDS - ELAPSED + 59) / 60 ))
        echo "[!] Previous SDE download was interrupted; waiting ${REMAINING}m before automated retry (run ./scripts/sync_sde.sh manually to resume now)."
        exit 0
    fi
fi

# 4. Preserve partial downloads matching REMOTE_MD5; purge stale partials from older builds
PART_GZ="data/.sde_part_${REMOTE_MD5}.gz"
TMP_DB="${DB_PATH}.tmp"
trap 'rm -f "$TMP_DB"' EXIT

for old_part in data/.sde_part_*.gz; do
    [ -e "$old_part" ] || continue
    if [ "$old_part" != "$PART_GZ" ]; then
        rm -f "$old_part"
    fi
done

if [ -f "$PART_GZ" ]; then
    CUR_MB=$(( $(wc -c < "$PART_GZ") / 1048576 ))
    echo "[*] Resuming incomplete EVE SDE download from ${CUR_MB} MB (${REMOTE_MD5})..."
elif [ -z "$LOCAL_MD5" ] || [ ! -s "$DB_PATH" ]; then
    echo "[*] Initial EVE SDE setup (${REMOTE_MD5}). Downloading..."
else
    echo "[*] New EVE SDE build detected (${LOCAL_MD5} -> ${REMOTE_MD5}). Downloading..."
fi

# 5. Download in an outer loop so each retry spawns a fresh `curl -C -` at the exact byte offset
DOWNLOAD_OK=false
for try in $(seq 1 "$MAX_CHUNK_RETRIES"); do
    if [ -f "$PART_GZ" ] && [ "$(calc_md5 "$PART_GZ")" = "$REMOTE_MD5" ]; then
        DOWNLOAD_OK=true
        break
    fi

    set +e
    curl -fL -C - \
        --connect-timeout 15 \
        --speed-limit 5120 \
        --speed-time 30 \
        -o "$PART_GZ" \
        "${BASE_URL}/${FILE_NAME}"
    CURL_EXIT=$?
    set -e

    # Check if file is actually complete (e.g. server returned 416 because 100% is already on disk)
    if [ -f "$PART_GZ" ] && [ "$(calc_md5 "$PART_GZ")" = "$REMOTE_MD5" ]; then
        DOWNLOAD_OK=true
        break
    fi

    if [ "$CURL_EXIT" -eq 33 ]; then
        echo "[!] Server rejected byte-range resume; resetting partial file..."
        rm -f "$PART_GZ"
    fi

    if [ "$try" -lt "$MAX_CHUNK_RETRIES" ]; then
        CUR_MB=0
        [ -f "$PART_GZ" ] && CUR_MB=$(( $(wc -c < "$PART_GZ") / 1048576 ))
        echo "[!] Transfer interrupted (curl exit ${CURL_EXIT}) at ${CUR_MB} MB. Resuming in 3s (attempt $((try + 1))/${MAX_CHUNK_RETRIES})..."
        sleep 3
    fi
done

if [ "$DOWNLOAD_OK" != "true" ]; then
    date +%s > "$FAIL_STAMP"
    echo "[!] Download did not complete after ${MAX_CHUNK_RETRIES} resume attempts. Partial file kept at ${PART_GZ}." >&2
    exit 1
fi

echo "[*] Checksum verified. Decompressing into ${DB_PATH}..."
gzip -dc "$PART_GZ" > "$TMP_DB"
mv "$TMP_DB" "$DB_PATH"
printf '%s\n' "$REMOTE_MD5" > "$MD5_CACHE"
rm -f "$PART_GZ" "$FAIL_STAMP"
echo "[+] EVE SDE ready (MD5: ${REMOTE_MD5})."

# 6. Run import if triggered by a poller
if [ "$MODE" = "--import-on-change" ]; then
    echo "[*] Running import pipeline for updated SDE build..."
    docker compose exec -T eve-trading-desk python3 -m workflows.daily_pipeline
fi
