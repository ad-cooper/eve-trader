#!/usr/bin/env python3
"""
FIFO Realized Trade Reconciler
==============================
Pairs historical sells against historical buys on a FIFO basis
and stores completed closed round-trips into DuckDB table `historical_realized_trades`.
"""

import os
import sys
import duckdb
from collections import deque
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

DUCKDB_PATH = "data/eve_analytics.duckdb"

# Jita 4-4 standard assumptions for historical accounting
EST_BUY_FEE = 0.0050   # 0.5% broker fee on buy order
EST_SELL_FEE = 0.04605 # 3.6% sales tax + 1.0% broker fee on sell

def reconcile_trades():
    con = duckdb.connect(DUCKDB_PATH)
    print("[*] Fetching transactions from DuckDB ordered chronologically...")
    
    rows = con.execute("""
        SELECT date, item_name, action, quantity, price, station, owner
        FROM historical_all_transactions
        ORDER BY date ASC
    """).fetchall()

    print(f"[*] Processing {len(rows):,} transactions through FIFO queue...")

    inventory = {} # item_name -> deque of [qty, price, date, station, owner]
    closed_trades = []

    for dt, item, action, qty, price, station, owner in rows:
        if action == "BUY":
            if item not in inventory:
                inventory[item] = deque()
            inventory[item].append({
                "qty": qty,
                "price": price,
                "date": dt,
                "station": station,
                "owner": owner
            })
        elif action == "SELL":
            qty_to_fill = qty
            if item not in inventory or not inventory[item]:
                # Short sell or acquired before transaction history began
                continue

            while qty_to_fill > 0 and inventory[item]:
                oldest_buy = inventory[item][0]
                matched_qty = min(qty_to_fill, oldest_buy["qty"])

                buy_cost_unit = oldest_buy["price"] * (1.0 + EST_BUY_FEE)
                sell_net_unit = price * (1.0 - EST_SELL_FEE)
                profit_unit = sell_net_unit - buy_cost_unit
                net_profit = profit_unit * matched_qty
                cost_basis = buy_cost_unit * matched_qty
                gross_proceeds = price * matched_qty

                hold_days = (dt - oldest_buy["date"]).total_seconds() / 86400.0

                closed_trades.append((
                    dt,
                    item,
                    matched_qty,
                    oldest_buy["price"],
                    price,
                    cost_basis,
                    gross_proceeds,
                    net_profit,
                    round(hold_days, 1),
                    oldest_buy["owner"],
                    owner
                ))

                oldest_buy["qty"] -= matched_qty
                qty_to_fill -= matched_qty

                if oldest_buy["qty"] <= 0:
                    inventory[item].popleft()

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

    con.executemany("""
        INSERT INTO historical_realized_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, closed_trades)

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
