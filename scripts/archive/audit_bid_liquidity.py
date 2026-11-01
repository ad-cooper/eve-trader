#!/usr/bin/env python3
"""
Jita Bid-Side Liquidity & Dump Auditor
Analyzes historical daily execution to eliminate one-sided producer markets.
"""

import duckdb
import pandas as pd

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"

con = duckdb.connect(DUCKDB_PATH, read_only=True)
con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")

print("[+] Auditing Jita 30-Day Execution History for Bid-Side Liquidity...")

query = """
WITH trailing_30d AS (
    SELECT 
        f.type_id,
        f.date,
        f.average,
        f.highest,
        f.lowest,
        f.volume,
        -- Full intraday range
        (f.highest - f.lowest) / NULLIF(f.average, 0) AS executed_spread,
        -- How far below average the lowest execution dumped
        (f.average - f.lowest) / NULLIF(f.average, 0) AS dump_depth
    FROM forge_market_history f
    WHERE f.date BETWEEN '2026-09-25' AND '2026-10-25'
      AND f.volume > 0
),
item_stats AS (
    SELECT 
        h.type_id,
        t.typeName AS item_name,
        g.groupName AS group_name,
        c.categoryName AS category_name,
        COUNT(h.date) AS days_traded,
        AVG(h.volume) AS avg_daily_vol,
        AVG(h.average) AS avg_price,
        AVG(h.executed_spread) * 100.0 AS avg_executed_spread_pct,
        AVG(h.dump_depth) * 100.0 AS avg_dump_depth_pct,
        -- Percentage of days where a dump >= 5% occurred
        (COUNT(CASE WHEN h.dump_depth >= 0.05 THEN 1 END) * 100.0 / COUNT(*)) AS dump_frequency_pct
    FROM trailing_30d h
    JOIN sde.invTypes t ON h.type_id = t.typeID
    JOIN sde.invGroups g ON t.groupID = g.groupID
    JOIN sde.invCategories c ON g.categoryID = c.categoryID
    WHERE t.published = 1
    GROUP BY h.type_id, t.typeName, g.groupName, c.categoryName
    HAVING COUNT(h.date) >= 15
)
SELECT 
    type_id,
    item_name,
    group_name,
    category_name,
    ROUND(avg_daily_vol, 1) AS daily_vol,
    ROUND(avg_price / 1e6, 2) AS price_m,
    ROUND(avg_executed_spread_pct, 1) AS avg_executed_spread,
    ROUND(avg_dump_depth_pct, 1) AS avg_dump_depth,
    ROUND(dump_frequency_pct, 1) AS dump_freq_pct,
    CASE 
        WHEN dump_frequency_pct < 30.0 OR avg_dump_depth_pct < 3.0 
            THEN 'RULE OUT: ONE-SIDED SELL TRAP'
        WHEN avg_executed_spread_pct < 9.0 
            THEN 'RULE OUT: TIGHT SPREAD'
        WHEN dump_frequency_pct >= 70.0 AND avg_dump_depth_pct >= 6.0 AND avg_executed_spread_pct >= 12.0 
            THEN 'PASS: HEALTHY TWO-SIDED LIQUIDITY'
        ELSE 'CAUTION: MODERATE LIQUIDITY'
    END AS liquidity_verdict
FROM item_stats
ORDER BY avg_executed_spread DESC;
"""

df = con.execute(query).df()
con.close()

# Export clean CSV
df.to_csv("data/jita_bid_liquidity_audit.csv", index=False)
print(f"[+] Processed {len(df)} active market items.")
print(f"[+] Verdict breakdown:")
print(df["liquidity_verdict"].value_counts().to_string())

# Display sample of One-Sided Sell Traps
print("\n" + "=" * 125)
print("--- SAMPLE ITEMS RULED OUT: ONE-SIDED SELL TRAPS (ZERO BUY-ORDER FLOW) ---")
print("=" * 125)
traps = df[df["liquidity_verdict"] == "RULE OUT: ONE-SIDED SELL TRAP"].sort_values(by="daily_vol", ascending=False).head(15)
cols = ["item_name", "group_name", "daily_vol", "price_m", "avg_executed_spread", "avg_dump_depth", "dump_freq_pct", "liquidity_verdict"]
print(traps[cols].to_string(index=False))

# Display sample of Verified Healthy Two-Sided Items
print("\n" + "=" * 125)
print("--- SAMPLE ITEMS PASSED: HEALTHY TWO-SIDED DUMP MARKETS ---")
print("=" * 125)
healthy = df[df["liquidity_verdict"] == "PASS: HEALTHY TWO-SIDED LIQUIDITY"].sort_values(by="daily_vol", ascending=False).head(15)
print(healthy[cols].to_string(index=False))
