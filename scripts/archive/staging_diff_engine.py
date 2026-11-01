#!/usr/bin/env python3
"""
Staging Snapshot Diffing Engine for DuckDB
Tracks exact player purchases and burn rates in Upwell structures.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List
import duckdb
import pandas as pd

DB_PATH = "data/eve_analytics.duckdb"

def init_diff_schema(con: duckdb.DuckDBPyConnection):
    """Initializes tables for order snapshots, sales events, and velocity views."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS staging_snapshots_meta (
            snapshot_id BIGINT PRIMARY KEY,
            structure_id BIGINT NOT NULL,
            scan_time TIMESTAMP WITH TIME ZONE NOT NULL,
            order_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staging_order_snapshots (
            snapshot_id BIGINT NOT NULL,
            structure_id BIGINT NOT NULL,
            order_id BIGINT NOT NULL,
            type_id INTEGER NOT NULL,
            price DOUBLE NOT NULL,
            volume_remain INTEGER NOT NULL,
            volume_total INTEGER NOT NULL,
            is_buy_order BOOLEAN NOT NULL,
            issued TIMESTAMP WITH TIME ZONE NOT NULL,
            duration INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staging_sales_events (
            structure_id BIGINT NOT NULL,
            type_id INTEGER NOT NULL,
            order_id BIGINT NOT NULL,
            event_time TIMESTAMP WITH TIME ZONE NOT NULL,
            prev_scan_time TIMESTAMP WITH TIME ZONE NOT NULL,
            quantity BIGINT NOT NULL,
            price DOUBLE NOT NULL,
            gross_isk DOUBLE NOT NULL,
            fill_type VARCHAR NOT NULL
        );

        CREATE OR REPLACE VIEW v_staging_sales_velocity AS
        WITH bounds AS (
            SELECT 
                structure_id,
                MIN(prev_scan_time) AS first_scan,
                MAX(event_time) AS latest_scan,
                GREATEST(0.1, date_diff('second', MIN(prev_scan_time), MAX(event_time)) / 86400.0) AS days_tracked
            FROM staging_sales_events
            GROUP BY structure_id
        )
        SELECT 
            s.structure_id,
            s.type_id,
            b.days_tracked,
            SUM(s.quantity) AS total_units_sold,
            ROUND(SUM(s.quantity) / b.days_tracked, 2) AS real_daily_burn,
            ROUND((SUM(s.quantity) / b.days_tracked) * 7.0, 1) AS real_weekly_burn,
            ROUND(SUM(s.gross_isk), 0) AS total_gross_isk,
            ROUND(SUM(s.gross_isk) / b.days_tracked, 0) AS real_daily_isk_burn
        FROM staging_sales_events s
        JOIN bounds b ON s.structure_id = b.structure_id
        GROUP BY s.structure_id, s.type_id, b.days_tracked;
    """)

def process_snapshot_diff(structure_id: int, raw_orders: List[Dict[str, Any]]):
    """Diffs the current market scan against the previous scan in DuckDB."""
    con = duckdb.connect(DB_PATH)
    init_diff_schema(con)

    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())

    # Build clean DataFrame of current sell orders
    current_sells = [
        {
            "order_id": int(o["order_id"]),
            "structure_id": structure_id,
            "type_id": int(o["type_id"]),
            "price": float(o["price"]),
            "volume_remain": int(o["volume_remain"]),
            "volume_total": int(o["volume_total"]),
            "is_buy_order": bool(o["is_buy_order"]),
            "issued": o["issued"].replace("T", " ").replace("Z", "+00"),
            "duration": int(o["duration"]),
        }
        for o in raw_orders if not o["is_buy_order"]
    ]

    df_new = pd.DataFrame(current_sells)
    if df_new.empty:
        con.close()
        return

    # Check for previous snapshot of this structure
    last_meta = con.execute("""
        SELECT snapshot_id, scan_time 
        FROM staging_snapshots_meta 
        WHERE structure_id = ? 
        ORDER BY scan_time DESC 
        LIMIT 1;
    """, [structure_id]).fetchall()

    if not last_meta:
        # First time scanning: record baseline
        con.execute("""
            INSERT INTO staging_snapshots_meta VALUES (?, ?, ?, ?);
        """, [now_epoch, structure_id, now, len(raw_orders)])

        df_new["snapshot_id"] = now_epoch
        con.register("df_initial", df_new)
        con.execute("""
            INSERT INTO staging_order_snapshots 
            SELECT snapshot_id, structure_id, order_id, type_id, price, 
                   volume_remain, volume_total, is_buy_order, 
                   CAST(issued AS TIMESTAMP WITH TIME ZONE), duration
            FROM df_initial;
        """)
        con.close()
        print(f"[+] Initial baseline snapshot saved ({len(df_new)} active sell orders).")
        print("[+] Future scans will automatically diff against this baseline to track exact sales velocity.")
        return

    prev_snapshot_id, prev_scan_time = last_meta[0]
    hours_since = (now - prev_scan_time).total_seconds() / 3600.0

    # Load previous sell orders
    df_old = con.execute("""
        SELECT order_id, type_id, price, volume_remain, volume_total, issued, duration
        FROM staging_order_snapshots
        WHERE snapshot_id = ?;
    """, [prev_snapshot_id]).df()

    events = []

    # 1. Partial Fills (Present in both scans, remaining volume decreased)
    merged = pd.merge(df_old, df_new, on=["order_id", "type_id"], suffixes=("_old", "_new"))
    partial_fills = merged[merged["volume_remain_old"] > merged["volume_remain_new"]]
    for _, r in partial_fills.iterrows():
        qty = int(r["volume_remain_old"] - r["volume_remain_new"])
        price = float(r["price_new"])
        events.append((structure_id, int(r["type_id"]), int(r["order_id"]), now, prev_scan_time, qty, price, qty * price, "PARTIAL_FILL"))

    # 2. Full Buyouts (Present in old, absent in new, and not expired)
    old_ids = set(df_old["order_id"])
    new_ids = set(df_new["order_id"])
    disappeared_ids = old_ids - new_ids
    if disappeared_ids:
        disappeared = df_old[df_old["order_id"].isin(disappeared_ids)]
        for _, r in disappeared.iterrows():
            issued_dt = pd.to_datetime(r["issued"], utc=True)
            expires_at = issued_dt + pd.to_timedelta(r["duration"], unit="D")
            if now < expires_at:
                qty = int(r["volume_remain"])
                price = float(r["price"])
                events.append((structure_id, int(r["type_id"]), int(r["order_id"]), now, prev_scan_time, qty, price, qty * price, "FULL_BUYOUT"))

    # 3. Intra-Scan Fills (New order posted and partially sold before current scan)
    new_only_ids = new_ids - old_ids
    if new_only_ids:
        new_only = df_new[df_new["order_id"].isin(new_only_ids)]
        intra_fills = new_only[new_only["volume_remain"] < new_only["volume_total"]]
        for _, r in intra_fills.iterrows():
            qty = int(r["volume_total"] - r["volume_remain"])
            price = float(r["price"])
            events.append((structure_id, int(r["type_id"]), int(r["order_id"]), now, prev_scan_time, qty, price, qty * price, "INTRA_SCAN_FILL"))

    # Log events to DuckDB
    if events:
        con.executemany("""
            INSERT INTO staging_sales_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, events)

    # Save new snapshot & update meta
    con.execute("INSERT INTO staging_snapshots_meta VALUES (?, ?, ?, ?);", [now_epoch, structure_id, now, len(raw_orders)])
    df_new["snapshot_id"] = now_epoch
    con.register("df_insert", df_new)
    con.execute("""
        INSERT INTO staging_order_snapshots 
        SELECT snapshot_id, structure_id, order_id, type_id, price, 
               volume_remain, volume_total, is_buy_order, 
               CAST(issued AS TIMESTAMP WITH TIME ZONE), duration
        FROM df_insert;
    """)

    # Keep only the last 3 snapshots in order table to prevent bloat
    con.execute("""
        DELETE FROM staging_order_snapshots 
        WHERE snapshot_id NOT IN (
            SELECT snapshot_id FROM staging_snapshots_meta 
            WHERE structure_id = ? 
            ORDER BY scan_time DESC 
            LIMIT 3
        );
    """, [structure_id])

    con.close()

    # Print summary
    total_qty = sum(e[5] for e in events)
    total_isk = sum(e[7] for e in events)
    print("\n" + "-" * 80)
    print(f"[+] Snapshot Diff against scan from {hours_since:.1f} hours ago:")
    print(f"    • Detected {len(events)} sales events across {len(set(e[1] for e in events))} item types")
    print(f"    • Total volume consumed: {total_qty:,} units")
    print(f"    • Total gross trade value: {total_isk / 1e9:.2f}B ISK")
    print("-" * 80 + "\n")
