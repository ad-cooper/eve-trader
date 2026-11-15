"""
Staging Snapshot Diffing Engine for DuckDB
Tracks exact player purchases and burn rates in Upwell structures over time.
Uses naive UTC TIMESTAMP columns so DuckDB never requires external pytz.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List
import duckdb
import pandas as pd

DB_PATH = "data/eve_analytics.duckdb"


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def init_diff_schema(con: duckdb.DuckDBPyConnection):
    """Initializes tables for order snapshots, sales events, and rolling 14d velocity views."""
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "staging_snapshots_meta" in tables:
        cnt = con.execute("SELECT COUNT(*) FROM staging_snapshots_meta").fetchone()[0]
        if cnt == 0:
            con.execute("""
                DROP VIEW IF EXISTS v_staging_sales_velocity;
                DROP TABLE IF EXISTS staging_sales_events;
                DROP TABLE IF EXISTS staging_order_snapshots;
                DROP TABLE IF EXISTS staging_snapshots_meta;
            """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS staging_snapshots_meta (
            snapshot_id BIGINT PRIMARY KEY,
            structure_id BIGINT NOT NULL,
            scan_time TIMESTAMP NOT NULL,
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
            issued TIMESTAMP NOT NULL,
            duration INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staging_sales_events (
            structure_id BIGINT NOT NULL,
            type_id INTEGER NOT NULL,
            order_id BIGINT NOT NULL,
            event_time TIMESTAMP NOT NULL,
            prev_scan_time TIMESTAMP NOT NULL,
            quantity BIGINT NOT NULL,
            price DOUBLE NOT NULL,
            gross_isk DOUBLE NOT NULL,
            fill_type VARCHAR NOT NULL
        );

        CREATE OR REPLACE VIEW v_staging_sales_velocity AS
        WITH bounds AS (
            SELECT
                structure_id,
                MIN(scan_time::TIMESTAMP) AS first_scan,
                MAX(scan_time::TIMESTAMP) AS latest_scan,
                COUNT(*) AS snapshot_count,
                GREATEST(0.0, date_diff('second', MIN(scan_time::TIMESTAMP), MAX(scan_time::TIMESTAMP)) / 86400.0) AS days_tracked
            FROM staging_snapshots_meta
            WHERE scan_time::TIMESTAMP >= ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::TIMESTAMP - INTERVAL 14 DAY)
            GROUP BY structure_id
        ),
        sales_14d AS (
            SELECT
                s.structure_id,
                s.type_id,
                SUM(s.quantity) AS total_units_sold,
                SUM(s.gross_isk) AS total_gross_isk,
                COUNT(*) AS event_count
            FROM staging_sales_events s
            JOIN bounds b ON s.structure_id = b.structure_id
            WHERE s.event_time::TIMESTAMP > b.first_scan
            GROUP BY s.structure_id, s.type_id
        )
        SELECT
            s.structure_id,
            s.type_id,
            b.days_tracked,
            b.snapshot_count,
            s.event_count,
            s.total_units_sold,
            CASE
                WHEN b.days_tracked >= 0.05 THEN ROUND(s.total_units_sold / b.days_tracked, 2)
                ELSE 0.0
            END AS real_daily_burn,
            CASE
                WHEN b.days_tracked >= 0.05 THEN ROUND((s.total_units_sold / b.days_tracked) * 7.0, 1)
                ELSE 0.0
            END AS real_weekly_burn,
            ROUND(s.total_gross_isk, 0) AS total_gross_isk,
            CASE
                WHEN b.days_tracked >= 0.05 THEN ROUND(s.total_gross_isk / b.days_tracked, 0)
                ELSE 0.0
            END AS real_daily_isk_burn
        FROM sales_14d s
        JOIN bounds b ON s.structure_id = b.structure_id;
    """)


def process_snapshot_diff(structure_id: int, raw_orders: List[Dict[str, Any]]) -> dict:
    """Diffs the current structure market scan against the previous scan in DuckDB."""
    con = duckdb.connect(DB_PATH)
    init_diff_schema(con)

    now = _utc_now_naive()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    current_sells = [
        {
            "order_id": int(o["order_id"]),
            "structure_id": structure_id,
            "type_id": int(o["type_id"]),
            "price": float(o["price"]),
            "volume_remain": int(o["volume_remain"]),
            "volume_total": int(o["volume_total"]),
            "is_buy_order": bool(o["is_buy_order"]),
            "issued": o["issued"].replace("T", " ").replace("Z", ""),
            "duration": int(o["duration"]),
        }
        for o in raw_orders if not o.get("is_buy_order")
    ]

    df_new = pd.DataFrame(current_sells)
    if df_new.empty:
        con.close()
        return {"baseline": False, "events_detected": 0, "units_sold": 0, "gross_isk": 0.0}

    last_meta = con.execute("""
        SELECT snapshot_id, scan_time::TIMESTAMP
        FROM staging_snapshots_meta
        WHERE structure_id = ?
        ORDER BY scan_time DESC
        LIMIT 1;
    """, [structure_id]).fetchall()

    if not last_meta:
        con.execute("INSERT INTO staging_snapshots_meta VALUES (?, ?, ?, ?);", [now_ms, structure_id, now, len(raw_orders)])
        df_new["snapshot_id"] = now_ms
        con.register("df_initial", df_new)
        con.execute("""
            INSERT INTO staging_order_snapshots
            SELECT snapshot_id, structure_id, order_id, type_id, price,
                   volume_remain, volume_total, is_buy_order,
                   CAST(issued AS TIMESTAMP), duration
            FROM df_initial;
        """)
        con.close()
        print(f"[+] Initial C-J6MT baseline snapshot saved ({len(df_new)} active sell orders).")
        return {"baseline": True, "events_detected": 0, "units_sold": 0, "gross_isk": 0.0}

    prev_snapshot_id, prev_scan_time = last_meta[0]
    if getattr(prev_scan_time, "tzinfo", None) is not None:
        prev_scan_time = prev_scan_time.replace(tzinfo=None)
    hours_since = (now - prev_scan_time).total_seconds() / 3600.0

    if hours_since < (1.0 / 60.0):
        con.close()
        return {"baseline": False, "skipped_too_soon": True, "events_detected": 0, "units_sold": 0, "gross_isk": 0.0}

    # If gap exceeds 12h (e.g. container downtime), reset baseline instead of counting stale cancellations as sales
    if hours_since > 12.0:
        con.execute("INSERT INTO staging_snapshots_meta VALUES (?, ?, ?, ?);", [now_ms, structure_id, now, len(raw_orders)])
        df_new["snapshot_id"] = now_ms
        con.register("df_reset", df_new)
        con.execute("""
            INSERT INTO staging_order_snapshots
            SELECT snapshot_id, structure_id, order_id, type_id, price,
                   volume_remain, volume_total, is_buy_order,
                   CAST(issued AS TIMESTAMP), duration
            FROM df_reset;
        """)
        con.close()
        print(f"[+] Gap was {hours_since:.1f}h (>12h) — saved fresh C-J6MT baseline snapshot.")
        return {"baseline": True, "reset_after_gap": round(hours_since, 2), "events_detected": 0, "units_sold": 0, "gross_isk": 0.0}

    df_old = con.execute("""
        SELECT order_id, type_id, price, volume_remain, volume_total, issued::TIMESTAMP AS issued, duration
        FROM staging_order_snapshots
        WHERE snapshot_id = ?;
    """, [prev_snapshot_id]).df()

    events = []

    # 1. Partial Fills
    merged = pd.merge(df_old, df_new, on=["order_id", "type_id"], suffixes=("_old", "_new"))
    partial_fills = merged[merged["volume_remain_old"] > merged["volume_remain_new"]]
    for _, r in partial_fills.iterrows():
        qty = int(r["volume_remain_old"] - r["volume_remain_new"])
        price = float(r["price_new"])
        events.append((structure_id, int(r["type_id"]), int(r["order_id"]), now, prev_scan_time, qty, price, qty * price, "PARTIAL_FILL"))

    # 2. Full Buyouts (not expired)
    old_ids = set(df_old["order_id"])
    new_ids = set(df_new["order_id"])
    disappeared_ids = old_ids - new_ids
    if disappeared_ids:
        disappeared = df_old[df_old["order_id"].isin(disappeared_ids)]
        for _, r in disappeared.iterrows():
            issued_dt = pd.to_datetime(r["issued"]).to_pydatetime().replace(tzinfo=None)
            expires_at = issued_dt + pd.to_timedelta(r["duration"], unit="D")
            if now < expires_at:
                qty = int(r["volume_remain"])
                price = float(r["price"])
                events.append((structure_id, int(r["type_id"]), int(r["order_id"]), now, prev_scan_time, qty, price, qty * price, "FULL_BUYOUT"))

    # 3. Intra-Scan Fills
    new_only_ids = new_ids - old_ids
    if new_only_ids:
        new_only = df_new[df_new["order_id"].isin(new_only_ids)]
        intra_fills = new_only[new_only["volume_remain"] < new_only["volume_total"]]
        for _, r in intra_fills.iterrows():
            qty = int(r["volume_total"] - r["volume_remain"])
            price = float(r["price"])
            events.append((structure_id, int(r["type_id"]), int(r["order_id"]), now, prev_scan_time, qty, price, qty * price, "INTRA_SCAN_FILL"))

    if events:
        con.executemany("INSERT INTO staging_sales_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);", events)

    con.execute("INSERT INTO staging_snapshots_meta VALUES (?, ?, ?, ?);", [now_ms, structure_id, now, len(raw_orders)])
    df_new["snapshot_id"] = now_ms
    con.register("df_insert", df_new)
    con.execute("""
        INSERT INTO staging_order_snapshots
        SELECT snapshot_id, structure_id, order_id, type_id, price,
               volume_remain, volume_total, is_buy_order,
               CAST(issued AS TIMESTAMP), duration
        FROM df_insert;
    """)

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

    total_qty = sum(e[5] for e in events)
    total_isk = sum(e[7] for e in events)
    print(
        f"[+] C-J6MT Snapshot Diff ({hours_since:.1f}h window): "
        f"{len(events)} sales events, {total_qty:,} units, {total_isk / 1e9:.2f}B ISK"
    )
    return {
        "baseline": False,
        "hours_since_prev": round(hours_since, 2),
        "events_detected": len(events),
        "units_sold": total_qty,
        "gross_isk": round(total_isk, 2),
    }


def get_observed_staging_velocity(structure_id: int) -> dict:
    """Returns tracking coverage and observed 14d daily burn rates per type_id for the structure."""
    try:
        con = duckdb.connect(DB_PATH, read_only=True)
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "staging_snapshots_meta" not in tables:
            con.close()
            return {"days_tracked": 0.0, "snapshot_count": 0, "by_type": {}}

        meta_row = con.execute("""
            SELECT
                COUNT(*) AS snap_cnt,
                COALESCE(date_diff('second', MIN(scan_time::TIMESTAMP), MAX(scan_time::TIMESTAMP)) / 86400.0, 0.0) AS days_tracked
            FROM staging_snapshots_meta
            WHERE structure_id = ?
              AND scan_time::TIMESTAMP >= ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::TIMESTAMP - INTERVAL 14 DAY);
        """, [structure_id]).fetchone()

        snap_cnt = int(meta_row[0] or 0) if meta_row else 0
        days_tracked = float(meta_row[1] or 0.0) if meta_row else 0.0

        by_type = {}
        if "v_staging_sales_velocity" in tables and days_tracked >= 0.05:
            rows = con.execute("""
                SELECT type_id, real_daily_burn, total_units_sold, event_count
                FROM v_staging_sales_velocity
                WHERE structure_id = ?;
            """, [structure_id]).fetchall()
            for tid, burn, sold, ev_cnt in rows:
                by_type[int(tid)] = {
                    "real_daily_burn": float(burn or 0.0),
                    "total_units_sold": int(sold or 0),
                    "event_count": int(ev_cnt or 0),
                }

        con.close()
        return {"days_tracked": round(days_tracked, 2), "snapshot_count": snap_cnt, "by_type": by_type}
    except Exception as e:
        print(f"[!] Could not read observed staging velocity: {e}")
        return {"days_tracked": 0.0, "snapshot_count": 0, "by_type": {}}
