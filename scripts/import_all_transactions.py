#!/usr/bin/env python3
"""
Full Historical Transaction Importer
===================================
Ingests both BUY and SELL transactions from jEveAssets CSV into DuckDB
for realized P&L trade matching.
"""

import os
import sys
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

DUCKDB_PATH = "data/eve_analytics.duckdb"
CSV_PATH = "data/jeveassets_transactions.csv"

def import_all():
    if not os.path.exists(CSV_PATH):
        print(f"[!] File not found: {CSV_PATH}")
        return

    print(f"[*] Reading {CSV_PATH}...")
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig", on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]

    df["clean_type"] = df["Type"].astype(str).str.strip().str.upper()
    valid_txs = df[df["clean_type"].isin(["BUY", "SELL"])].copy()

    valid_txs["clean_price"] = pd.to_numeric(valid_txs["Price"].astype(str).str.replace(",", "").str.strip(), errors="coerce")
    valid_txs["clean_qty"] = pd.to_numeric(valid_txs["Quantity"].astype(str).str.replace(",", "").str.strip(), errors="coerce")
    valid_txs["clean_date"] = pd.to_datetime(valid_txs["Date"], errors="coerce")
    valid_txs["clean_name"] = valid_txs["Name"].astype(str).str.strip()
    valid_txs["clean_station"] = valid_txs["Station"].astype(str).str.strip() if "Station" in valid_txs.columns else "Unknown"
    valid_txs["clean_owner"] = valid_txs["Owner"].astype(str).str.strip() if "Owner" in valid_txs.columns else "Unknown"

    valid_txs = valid_txs.dropna(subset=["clean_price", "clean_qty", "clean_date"])
    valid_txs = valid_txs[(valid_txs["clean_price"] > 0) & (valid_txs["clean_qty"] > 0)].copy()

    con = duckdb.connect(DUCKDB_PATH)
    con.execute("""
    CREATE TABLE IF NOT EXISTS historical_all_transactions (
        date TIMESTAMP,
        item_name VARCHAR,
        action VARCHAR,
        quantity BIGINT,
        price DOUBLE,
        station VARCHAR,
        owner VARCHAR
    );
    """)

    con.execute("DELETE FROM historical_all_transactions")

    clean_df = pd.DataFrame({
        "date": valid_txs["clean_date"],
        "item_name": valid_txs["clean_name"],
        "action": valid_txs["clean_type"],
        "quantity": valid_txs["clean_qty"].astype(int),
        "price": valid_txs["clean_price"],
        "station": valid_txs["clean_station"],
        "owner": valid_txs["clean_owner"]
    })

    con.register("df_raw", clean_df)
    con.execute("INSERT INTO historical_all_transactions SELECT * FROM df_raw")

    buys = con.execute("SELECT COUNT(*) FROM historical_all_transactions WHERE action = 'BUY'").fetchone()[0]
    sells = con.execute("SELECT COUNT(*) FROM historical_all_transactions WHERE action = 'SELL'").fetchone()[0]
    con.close()

    print(f"[+] Loaded {buys:,} Buys and {sells:,} Sells into historical_all_transactions.")

if __name__ == "__main__":
    import_all()
