#!/usr/bin/env python3
"""
Bulk Market History Synchronizer (Archive Ingestion Engine)
===========================================================
- Checks data/eve_analytics.duckdb for the latest recorded date.
- Downloads daily market-history dumps (.csv.bz2) from data.everef.net.
- Decompresses via Python's built-in bz2 module into a temporary CSV.
- Filters directly for The Forge (10000002) and ingests into DuckDB.
"""

import os
import sys
import bz2
import datetime
import tempfile
import duckdb
import requests

DUCKDB_PATH = "data/eve_analytics.duckdb"
REGION_THE_FORGE = 10000002
BASE_DUMP_URL = "https://data.everef.net/market-history"

def get_db_connection():
    os.makedirs("data", exist_ok=True)
    con = duckdb.connect(DUCKDB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS forge_market_history (
            date DATE,
            type_id INTEGER,
            average DOUBLE,
            highest DOUBLE,
            lowest DOUBLE,
            volume BIGINT,
            order_count BIGINT,
            PRIMARY KEY (date, type_id)
        );
    """)
    return con

def get_missing_dates(con):
    res = con.execute("SELECT MAX(date) FROM forge_market_history;").fetchone()[0]
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    
    if res is None:
        start_date = today - datetime.timedelta(days=90)
        print(f"[*] Database is empty. Bootstrapping trailing 90 days from {start_date}...")
    else:
        if isinstance(res, str):
            last_date = datetime.datetime.strptime(res, "%Y-%m-%d").date()
        elif isinstance(res, datetime.date):
            last_date = res
        else:
            last_date = res.date()
            
        start_date = last_date + datetime.timedelta(days=1)
        
    if start_date > yesterday:
        return []
        
    missing = []
    curr = start_date
    while curr <= yesterday:
        missing.append(curr)
        curr += datetime.timedelta(days=1)
    return missing

def ingest_day(con, day: datetime.date):
    year = day.strftime("%Y")
    date_str = day.strftime("%Y-%m-%d")
    url = f"{BASE_DUMP_URL}/{year}/market-history-{date_str}.csv.bz2"
    
    print(f"[*] Fetching dump for {date_str}...")
    headers = {"User-Agent": "EVE-Market-Sync/2.0"}
    
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=60)
        if resp.status_code == 404:
            print(f"[-] Dump for {date_str} is not yet available on archive (404).")
            return False
        elif resp.status_code != 200:
            print(f"[-] HTTP error {resp.status_code} fetching dump for {date_str}.")
            return False

        # Decompress into a temporary local CSV
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = tmp.name
            decompressor = bz2.BZ2Decompressor()
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp.write(decompressor.decompress(chunk))

        # Ingest uncompressed CSV using DuckDB native reader
        query = f"""
            INSERT OR REPLACE INTO forge_market_history
            SELECT 
                CAST(date AS DATE) AS date,
                CAST(type_id AS INTEGER) AS type_id,
                CAST(average AS DOUBLE) AS average,
                CAST(highest AS DOUBLE) AS highest,
                CAST(lowest AS DOUBLE) AS lowest,
                CAST(volume AS BIGINT) AS volume,
                CAST(order_count AS BIGINT) AS order_count
            FROM read_csv('{tmp_path}', 
                          header=True, 
                          auto_detect=True)
            WHERE region_id = {REGION_THE_FORGE};
        """
        con.execute(query)
        os.remove(tmp_path)
        print(f"[+] Successfully ingested {date_str} into DuckDB.")
        return True
        
    except Exception as e:
        if "tmp_path" in locals() and os.path.exists(tmp_path):
            os.remove(tmp_path)
        print(f"[-] Failed to ingest {date_str}: {e}")
        return False

def main():
    con = get_db_connection()
    missing_dates = get_missing_dates(con)
    
    if not missing_dates:
        print("[+] Database is already up to date with the latest available market history.")
        con.close()
        return

    print(f"[+] Found {len(missing_dates)} missing day(s) to sync ({missing_dates[0]} to {missing_dates[-1]}).")
    for day in missing_dates:
        ingest_day(con, day)
        
    total_rows = con.execute("SELECT COUNT(*) FROM forge_market_history;").fetchone()[0]
    latest = con.execute("SELECT MAX(date) FROM forge_market_history;").fetchone()[0]
    con.close()
    print(f"\n[+] Sync complete! Total records in forge_market_history: {total_rows:,} (Latest Date: {latest})")

if __name__ == "__main__":
    main()
