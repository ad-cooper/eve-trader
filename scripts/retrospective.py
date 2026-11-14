import json
import duckdb
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb", read_only=True)
tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
print("Available DuckDB tables:", sorted(tables))

# 1. Forward Market Backtest of Past Ledger Selections
if "ledger_history" in tables and "forge_market_history" in tables:
    snap_info = con.execute("""
        SELECT COUNT(DISTINCT snapshot_time), MIN(snapshot_time), MAX(snapshot_time), COUNT(*)
        FROM ledger_history
    """).fetchone()
    print(f"\n=== 1. LEDGER HISTORY COVERAGE ===")
    print(f"Snapshots: {snap_info[0]} | Range: {snap_info[1]} -> {snap_info[2]} | Total Rows: {snap_info[3]:,}")

    backtest = con.execute("""
        WITH horizons AS (
            SELECT 
                lh.snapshot_time,
                CAST(lh.snapshot_time AS DATE) AS snap_date,
                lh.desk_type,
                lh.type_id,
                lh.name,
                lh.target_buy,
                lh.target_sell,
                lh.net_margin AS est_margin,
                lh.capital,
                lh.profit AS est_profit,
                CASE lh.desk_type
                    WHEN 'evergreen' THEN 3
                    WHEN 'tactical' THEN 7
                    ELSE 21
                END AS horizon_days
            FROM ledger_history lh
            WHERE CAST(lh.snapshot_time AS DATE) <= (SELECT MAX(date) - INTERVAL 3 DAY FROM forge_market_history)
        ),
        eval AS (
            SELECT 
                h.*,
                MIN(m.lowest) AS min_low_after,
                MAX(m.highest) AS max_high_after,
                SUM(m.volume) AS vol_after,
                MIN(CASE WHEN m.lowest <= h.target_buy THEN m.date END) AS first_fill_date
            FROM horizons h
            LEFT JOIN forge_market_history m
              ON m.type_id = h.type_id
             AND m.date > h.snap_date
             AND m.date <= h.snap_date + (h.horizon_days * INTERVAL 1 DAY)
            GROUP BY ALL
        ),
        exit_check AS (
            SELECT 
                e.*,
                MAX(CASE WHEN m2.highest >= e.target_sell THEN 1 ELSE 0 END) AS hit_target_sell
            FROM eval e
            LEFT JOIN forge_market_history m2
              ON m2.type_id = e.type_id
             AND e.first_fill_date IS NOT NULL
             AND m2.date >= e.first_fill_date
             AND m2.date <= e.snap_date + (e.horizon_days * INTERVAL 1 DAY)
            GROUP BY ALL
        )
        SELECT 
            desk_type,
            COUNT(*) AS picks,
            ROUND(AVG(est_margin), 1) AS avg_est_margin_pct,
            ROUND(AVG(CASE WHEN first_fill_date IS NOT NULL THEN 100.0 ELSE 0.0 END), 1) AS buy_fill_rate_pct,
            ROUND(AVG(CASE WHEN first_fill_date IS NOT NULL AND hit_target_sell = 1 THEN 100.0 ELSE 0.0 END), 1) AS full_roundtrip_pct,
            ROUND(SUM(CASE WHEN first_fill_date IS NOT NULL AND hit_target_sell = 1 THEN est_profit ELSE 0 END) / 1e9, 2) AS realized_model_profit_b
        FROM exit_check
        GROUP BY desk_type
        ORDER BY full_roundtrip_pct DESC
    """).df()
    print("\n--- Forward Market Backtest by Desk (within target horizon) ---")
    print(backtest.to_string(index=False))

    # Worst false-positive picks (never filled or filled and never recovered)
    misses = con.execute("""
        WITH recent AS (
            SELECT 
                CAST(lh.snapshot_time AS DATE) AS snap_date,
                lh.desk_type, lh.type_id, lh.name, lh.target_buy, lh.target_sell, lh.capital
            FROM ledger_history lh
            WHERE CAST(lh.snapshot_time AS DATE) <= (SELECT MAX(date) - INTERVAL 7 DAY FROM forge_market_history)
        )
        SELECT 
            r.desk_type, r.name,
            COUNT(*) AS times_picked,
            ROUND(AVG(r.target_buy)/1e6, 1) AS avg_buy_m,
            ROUND(AVG(r.target_sell)/1e6, 1) AS avg_sell_m,
            ROUND(MIN(m.lowest)/1e6, 1) AS actual_min_m,
            ROUND(MAX(m.highest)/1e6, 1) AS actual_max_m
        FROM recent r
        JOIN forge_market_history m
          ON m.type_id = r.type_id AND m.date BETWEEN r.snap_date + 1 AND r.snap_date + 7
        GROUP BY r.desk_type, r.name
        HAVING MIN(m.lowest) > AVG(r.target_buy) OR MAX(m.highest) < AVG(r.target_sell)
        ORDER BY times_picked DESC
        LIMIT 12
    """).df()
    print("\n--- Frequent Misses (Target Buy Too Low or Target Sell Too High) ---")
    print(misses.to_string(index=False))

# 2. Realized Trades in Practice: Ledger Picks vs. Non-Ledger Picks
try:
    with open("ledgers/performance_cache.json") as f:
        perf = json.load(f)
    items = perf.get("items", [])
    if items and "ledger_history" in tables:
        ledger_tids = {
            r[0]: r[1] for r in con.execute("""
                SELECT type_id, MAX(desk_type) FROM ledger_history GROUP BY type_id
            """).fetchall()
        }
        rows = []
        for it in items:
            tid = int(it["type_id"])
            src = ledger_tids.get(tid, "OFF_LEDGER (PRACTICE)")
            cost = float(it.get("cost", 0.0))
            prof = float(it.get("profit", 0.0))
            rows.append({
                "source": src,
                "name": it["name"],
                "qty_sold": it["qty_sold"],
                "cost_m": cost / 1e6,
                "profit_m": prof / 1e6,
                "roi_pct": (prof / cost * 100.0) if cost > 0 else 0.0
            })
        df_real = pd.DataFrame(rows)
        print("\n=== 2. REALIZED 30D P&L: LEDGER PICKS VS. OFF-LEDGER IN PRACTICE ===")
        summary = df_real.groupby("source").agg(
            unique_items=("name", "count"),
            capital_turned_b=("cost_m", lambda s: round(s.sum() / 1e3, 2)),
            net_profit_b=("profit_m", lambda s: round(s.sum() / 1e3, 2)),
            median_roi_pct=("roi_pct", lambda s: round(s.median(), 1))
        ).reset_index()
        print(summary.to_string(index=False))

        print("\n--- Top 10 Realized Winners Overall (Check Source Column) ---")
        print(df_real.sort_values("profit_m", ascending=False).head(10).to_string(index=False))

        print("\n--- Worst 10 Realized Losers / Margin Traps ---")
        print(df_real.sort_values("profit_m", ascending=True).head(10).to_string(index=False))
except Exception as e:
    print(f"[!] Could not load performance_cache.json: {e}")

con.close()
