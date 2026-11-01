#!/usr/bin/env python3
"""
Everef 2026 Market History Sync with Ledger Tracking
Streams .csv.bz2 archives, filters for The Forge, and logs progress into DuckDB.
"""

import io
import bz2
import requests
import duckdb
import pandas as pd

DB_PATH = "data/eve_analytics.duckdb"
REGION_THE_FORGE = 10000002
INDEX_URL = "https://data.everef.net/market-history/2026/index.json"
HEADERS = {"User-Agent": "EVE-Market-Sync/1.0"}

def init_tracking_ledger(con: duckdb.DuckDBPyConnection):
    """Initializes the ledger table and seeds it with existing 2025 records."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS market_history_import_log (
            import_date DATE PRIMARY KEY,
            records_imported BIGINT NOT NULL,
            imported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # Lock in the existing 2025 history so it is never re-processed
    con.execute("""
        INSERT OR IGNORE INTO market_history_import_log (import_date, records_imported)
        SELECT 
            date, 
            COUNT(*) 
        FROM forge_market_history 
        GROUP BY date;
    """)

def sync_market_history():
    con = duckdb.connect(DB_PATH)
    init_tracking_ledger(con)

    # 1. Check ledger for completed dates
    completed_dates = set(
        str(r[0]) for r in con.execute("SELECT import_date FROM market_history_import_log;").fetchall()
    )
    print(f"[+] Ledger initialized: {len(completed_dates)} dates locked in DB.")

    # 2. Fetch available files from Everef's index
    print(f"[+] Fetching 2026 file index from Everef...")
    resp = requests.get(INDEX_URL, headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        print(f"[-] Failed to fetch index: HTTP {resp.status_code}")
        con.close()
        return

    all_files = resp.json().get("files", [])
    
    # Filter for files that haven't been imported yet
    pending_files = []
    for f in all_files:
        name = f.get("name", "")
        if name.endswith(".csv.bz2"):
            # Extract date string 'YYYY-MM-DD' from filename
            date_str = name.replace("market-history-", "").replace(".csv.bz2", "")
            if date_str not in completed_dates:
                file_url = f.get("url") or f"https://data.everef.net/market-history/2026/{name}"
                pending_files.append((date_str, file_url))

    print(f"[+] Total files in 2026 index: {len(all_files)}")
    print(f"[+] Missing dates to download & import: {len(pending_files)}\n")

    if not pending_files:
        print("[+] All 2026 dates are already synced.")
        con.close()
        return

    session = requests.Session()
    session.headers.update(HEADERS)

    # 3. Stream, decompress, and insert missing days
    for idx, (date_str, file_url) in enumerate(pending_files, 1):
        print(f"[{idx:>3}/{len(pending_files)}] Syncing {date_str}...", end=" ", flush=True)

        try:
            r = session.get(file_url, stream=True, timeout=20)
            if r.status_code != 200:
                print(f"FAILED (HTTP {r.status_code})")
                continue

            # Decompress in memory
            raw_csv = bz2.decompress(r.content)
            
            # Read into dataframe and filter strictly for The Forge
            df = pd.read_csv(io.BytesIO(raw_csv))
            forge_df = df[df["region_id"] == REGION_THE_FORGE].copy()

            if forge_df.empty:
                print("SKIPPED (0 Forge records)")
                continue

            # Register temporary view and insert
            con.register("forge_df_view", forge_df)
            con.execute("""
                INSERT INTO forge_market_history (date, type_id, average, highest, lowest, volume, order_count)
                SELECT 
                    CAST(date AS DATE) AS date,
                    type_id,
                    average,
                    highest,
                    lowest,
                    volume,
                    order_count
                FROM forge_df_view;
            """)
            con.unregister("forge_df_view")

            records_count = len(forge_df)

            # Record in ledger
            con.execute("""
                INSERT INTO market_history_import_log (import_date, records_imported)
                VALUES (CAST(? AS DATE), ?);
            """, [date_str, records_count])

            print(f"DONE (+{records_count:,} Forge items)")

        except Exception as e:
            print(f"ERROR: {e}")
            break

    # 4. Final summary
    latest = con.execute("SELECT MAX(date) FROM forge_market_history;").fetchone()[0]
    total_rows = con.execute("SELECT COUNT(*) FROM forge_market_history;").fetchone()[0]
    con.close()

    print("\n" + "=" * 65)
    print(f"[+] Sync Process Finished:")
    print(f"    • New latest date in DB: {latest}")
    print(f"    • Total table rows in forge_market_history: {total_rows:,}")
    print("=" * 65)

if __name__ == "__main__":
    sync_market_history()
