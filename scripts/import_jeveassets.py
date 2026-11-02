#!/usr/bin/env python3
"""
jEveAssets Transaction Importer
===============================
Imports lifetime transaction history from jEveAssets CSV into DuckDB
using the exact column schema:
  - Name -> item_name
  - Type -> Buy / Sell
  - Quantity -> quantity
  - Price -> unit buy price
  - Date -> date
  - Station -> station
  - Owner -> owner (character/corporation)
"""

import os
import sys
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

DUCKDB_PATH = "data/eve_analytics.duckdb"
CSV_PATH = "data/jeveassets_transactions.csv"

def import_transactions():
    if not os.path.exists(CSV_PATH):
        print(f"[!] File not found: {CSV_PATH}")
        return False

    print(f"[*] Reading {CSV_PATH}...")
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig", on_bad_lines="skip")
    print(f"[*] Total rows in export: {len(df):,}")

    # Standardize column headers
    df.columns = [c.strip() for c in df.columns]

    # Filter strictly for Buy transactions
    if "Type" not in df.columns:
        print(f"[!] Could not find 'Type' column in headers: {list(df.columns)}")
        return False

    buys = df[df["Type"].astype(str).str.strip().str.lower() == "buy"].copy()
    print(f"[*] Filtered {len(buys):,} BUY transactions.")

    # Clean price & quantity
    buys["clean_price"] = pd.to_numeric(buys["Price"].astype(str).str.replace(",", "").str.strip(), errors="coerce")
    buys["clean_qty"] = pd.to_numeric(buys["Quantity"].astype(str).str.replace(",", "").str.strip(), errors="coerce")
    buys["clean_date"] = pd.to_datetime(buys["Date"], errors="coerce")
    buys["clean_name"] = buys["Name"].astype(str).str.strip()
    buys["clean_station"] = buys["Station"].astype(str).str.strip() if "Station" in buys.columns else "Unknown"
    buys["clean_owner"] = buys["Owner"].astype(str).str.strip() if "Owner" in buys.columns else "Unknown"

    # Drop nulls / non-positive prices
    buys = buys.dropna(subset=["clean_price", "clean_qty", "clean_date"])
    buys = buys[(buys["clean_price"] > 0) & (buys["clean_qty"] > 0)].copy()

    # Create table in DuckDB
    con = duckdb.connect(DUCKDB_PATH)
    con.execute("""
    CREATE TABLE IF NOT EXISTS historical_wallet_buys (
        date TIMESTAMP,
        item_name VARCHAR,
        price DOUBLE,
        quantity BIGINT,
        station VARCHAR,
        owner VARCHAR,
        source VARCHAR DEFAULT 'JEVEASSETS'
    );
    """)

    # Clean existing jEveAssets records before refreshing
    con.execute("DELETE FROM historical_wallet_buys WHERE source = 'JEVEASSETS'")

    final_df = pd.DataFrame({
        "date": buys["clean_date"],
        "item_name": buys["clean_name"],
        "price": buys["clean_price"],
        "quantity": buys["clean_qty"].astype(int),
        "station": buys["clean_station"],
        "owner": buys["clean_owner"],
        "source": "JEVEASSETS"
    })

    con.register("df_final", final_df)
    con.execute("INSERT INTO historical_wallet_buys SELECT * FROM df_final")

    total_records = con.execute("SELECT COUNT(*) FROM historical_wallet_buys").fetchone()[0]
    sample = con.execute("""
        SELECT date, owner, item_name, quantity, price 
        FROM historical_wallet_buys 
        ORDER BY date DESC 
        LIMIT 4
    """).fetchall()
    con.close()

    print(f"[+] Loaded {len(final_df):,} buy records into DuckDB. Total records in database: {total_records:,}")
    print("[*] Sample loaded buys:")
    for r in sample:
        d_str = r[0].strftime("%Y-%m-%d %H:%M") if hasattr(r[0], "strftime") else str(r[0])
        print(f"    {d_str} | {r[1]} | {r[2]} | {r[3]:,}x @ {r[4]:,.2f} ISK")
    return True

if __name__ == "__main__":
    import_transactions()
