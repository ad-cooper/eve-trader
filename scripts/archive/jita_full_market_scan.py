#!/usr/bin/env python3
"""
Comprehensive Jita 4-4 Market Sector Analyzer
Joins DuckDB trade history with the SDE SQLite database.
"""

import duckdb

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"

con = duckdb.connect(DUCKDB_PATH, read_only=True)

# Attach the EVE SDE database from SQLite
con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")

print("[+] Attached SDE database successfully.")
print("[+] Running multi-sector market scan across 2025 history...")

query = """
WITH jita_aggregates AS (
    SELECT 
        type_id,
        COUNT(date) AS days_traded,
        ROUND(AVG(volume), 1) AS avg_daily_vol,
        ROUND(AVG(average), 2) AS avg_price,
        ROUND(AVG(volume * average) / 1e6, 2) AS avg_daily_m,
        -- Robust spread metric: (Highest - Lowest) / Average Price
        ROUND(AVG((highest - lowest) / NULLIF(average, 0)) * 100, 1) AS robust_daily_spread_pct
    FROM forge_market_history
    WHERE date >= '2025-01-01' AND date <= '2025-12-31'
      AND lowest > 100.0  -- Exclude 1-ISK glitch transactions
    GROUP BY type_id
    HAVING COUNT(date) >= 200          -- Actively traded at least ~55% of the year
       AND AVG(volume * average) >= 1e7 -- Minimum 10M ISK/day turnover (widens scope)
       AND AVG(volume) >= 5.0           -- Minimum 5 units/day
),
enriched_market AS (
    SELECT 
        j.type_id,
        t.typeName AS item_name,
        c.categoryName AS category_name,
        g.groupName AS group_name,
        j.days_traded,
        j.avg_daily_vol,
        ROUND(j.avg_price / 1e6, 2) AS avg_price_m,
        j.avg_daily_m,
        j.robust_daily_spread_pct,
        -- Estimated net margin after 4.6% Jita broker fee and sales tax
        ROUND(j.robust_daily_spread_pct - 4.6, 1) AS est_net_spread_pct,
        -- Estimated daily net profit capacity
        ROUND((j.avg_daily_m * (j.robust_daily_spread_pct - 4.6) / 100.0), 2) AS est_daily_profit_m
    FROM jita_aggregates j
    JOIN sde.invTypes t ON j.type_id = t.typeID
    JOIN sde.invGroups g ON t.groupID = g.groupID
    JOIN sde.invCategories c ON g.categoryID = c.categoryID
    WHERE t.published = 1
      AND j.robust_daily_spread_pct >= 10.0  -- Minimum 10% daily spread
      AND c.categoryName NOT IN ('Blueprint', 'Skins', 'Apparel', 'Special Edition Assets')
)
SELECT * FROM enriched_market
ORDER BY est_daily_profit_m DESC;
"""

df_all = con.execute(query).df()
print(f"[+] Total profitable market items identified: {len(df_all)}")

# Print Sector Summary
print("\n" + "=" * 90)
print(f"{'CATEGORY':<28} | {'ACTIVE ITEMS':<12} | {'AVG DAILY ISK (M)':<18} | {'AVG SPREAD %'}")
print("=" * 90)
sector_summary = (
    df_all.groupby("category_name")
    .agg(
        item_count=("type_id", "count"),
        total_daily_m=("avg_daily_m", "mean"),
        avg_spread=("robust_daily_spread_pct", "mean"),
    )
    .sort_values(by="item_count", ascending=False)
)

for cat, r in sector_summary.iterrows():
  print(
      f"{cat:<28} | {int(r['item_count']):>12} | {r['total_daily_m']:>18.1f}M |"
      f" {r['avg_spread']:>11.1f}%"
  )
print("=" * 90)

# Display Top Non-Implant Opportunities
print("\n--- TOP 35 HIGH-YIELD OPPORTUNITIES (EXCLUDING CYBERNETICS) ---")
non_implants = df_all[df_all["category_name"] != "Implant"].head(35)
cols_show = [
    "item_name",
    "group_name",
    "avg_daily_vol",
    "avg_price_m",
    "avg_daily_m",
    "robust_daily_spread_pct",
    "est_daily_profit_m",
]
print(non_implants[cols_show].to_string(index=False))

# Export entire market breakdown to CSV
con.execute(f"""
    COPY ({query}) TO 'data/jita_all_sectors_analyzed.csv' (HEADER, DELIMITER ',');
""")
print(
    "\n[+] Full market analysis exported to: data/jita_all_sectors_analyzed.csv"
)
