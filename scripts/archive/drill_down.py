import duckdb
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH 'data/latest-sqlite.db' AS sde (TYPE SQLITE);")

# We target the most promising structural groups from your scan
TARGET_GROUPS = [
    'Large Shield Rigs',
    'Medium Missile Launcher Rigs',
    'Capital Astronautic Rigs',
    'Ice Harvesters',
    'Strip Miners',
    'Metallic Nanocoatings',
    'Thermal Energized Membranes'
]

placeholders = ", ".join([f"'{g}'" for g in TARGET_GROUPS])

query = f"""
WITH item_metrics AS (
    SELECT 
        type_id,
        COUNT(*) as days_active,
        AVG(volume) as avg_daily_vol,
        AVG(volume * average) as avg_daily_isk,
        QUANTILE_CONT(average, 0.15) as p15_buy,
        QUANTILE_CONT(average, 0.85) as p85_sell,
        MEDIAN(average) as median_price
    FROM forge_market_history
    GROUP BY type_id
    HAVING days_active >= 20 AND avg_daily_isk >= 150000000 -- At least 150M ISK/day
)
SELECT 
    mg.marketGroupName as group_name,
    t.typeName as item_name,
    ROUND(m.avg_daily_vol, 0) as daily_units,
    ROUND(m.avg_daily_isk / 1e6, 0) as daily_isk_m,
    ROUND(m.p15_buy, 0) as p15_buy,
    ROUND(m.p85_sell, 0) as p85_sell,
    ROUND((m.p85_sell - m.p15_buy) / m.median_price * 100, 1) as spread_pct
FROM item_metrics m
JOIN sde.invTypes t ON m.type_id = t.typeID
JOIN sde.invMarketGroups mg ON t.marketGroupID = mg.marketGroupID
WHERE mg.marketGroupName IN ({placeholders})
ORDER BY group_name, spread_pct DESC;
"""

df = con.execute(query).df()
pd.set_option('display.max_rows', 100)
pd.set_option('display.width', 1000)
print("\n=== INDIVIDUAL SWING TARGETS INSIDE DISCOVERED GROUPS ===")
print(df.to_string(index=False))
