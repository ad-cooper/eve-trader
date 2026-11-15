#!/usr/bin/env python3
"""
FIFO Realized Trade Reconciler
==============================
Pairs historical sells against historical buys on a FIFO basis using
services.accounting.match_fifo_lots and stores completed round-trips in DuckDB.
"""

import os
import sys
import duckdb

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.accounting import DUCKDB_PATH, match_fifo_lots


def reconcile_trades():
    con = duckdb.connect(DUCKDB_PATH)
    print("[*] Fetching transactions from DuckDB ordered chronologically...")

    rows = con.execute("""
        SELECT date, item_name, action, quantity, price, station, owner
        FROM historical_all_transactions
        ORDER BY date ASC
    """).fetchall()

    print(f"[*] Processing {len(rows):,} transactions through centralized FIFO engine...")

    events = [
        {
            "key": item,
            "date": dt,
            "is_buy": (action == "BUY"),
            "qty": qty,
            "price": price,
            "meta": {"station": station, "owner": owner},
        }
        for dt, item, action, qty, price, station, owner in rows
        if action in ("BUY", "SELL")
    ]

    matched_lots = match_fifo_lots(events, unmatched_cost_ratio=None)
    closed_trades = [
        (
            lot["close_date"],
            lot["key"],
            lot["qty"],
            lot["buy_price"],
            lot["sell_price"],
            lot["cost_basis"],
            lot["gross_proceeds"],
            lot["net_profit"],
            lot["hold_days"],
            lot["buy_meta"].get("owner"),
            lot["sell_meta"].get("owner"),
        )
        for lot in matched_lots
    ]

    print(f"[+] Reconciled {len(closed_trades):,} completed round-trip trades.")

    con.execute("""
    CREATE TABLE IF NOT EXISTS historical_realized_trades (
        close_date TIMESTAMP,
        item_name VARCHAR,
        quantity BIGINT,
        buy_price DOUBLE,
        sell_price DOUBLE,
        cost_basis DOUBLE,
        gross_proceeds DOUBLE,
        net_profit DOUBLE,
        hold_days DOUBLE,
        buy_owner VARCHAR,
        sell_owner VARCHAR
    );
    """)

    con.execute("DELETE FROM historical_realized_trades")
    con.executemany(
        "INSERT INTO historical_realized_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        closed_trades,
    )

    summary = con.execute("""
        SELECT
            COUNT(*) as total_trades,
            ROUND(SUM(cost_basis), 2) as total_invested,
            ROUND(SUM(gross_proceeds), 2) as total_revenue,
            ROUND(SUM(net_profit), 2) as total_net_profit,
            ROUND(AVG(net_profit / NULLIF(cost_basis, 0)) * 100, 2) as avg_margin_pct
        FROM historical_realized_trades
    """).fetchone()

    con.close()

    print("\n--- Lifetime Realized Performance ---")
    print(f"Closed Trades:      {summary[0]:,}")
    print(f"Capital Invested:   {summary[1]:,.2f} ISK")
    print(f"Gross Revenue:      {summary[2]:,.2f} ISK")
    print(f"Total Net Profit:   {summary[3]:,.2f} ISK")
    print(f"Average Margin:     {summary[4]:.2f}%")


if __name__ == "__main__":
    reconcile_trades()
