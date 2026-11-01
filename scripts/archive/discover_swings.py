import duckdb
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")

# Attach the SQLite SDE
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH 'data/latest-sqlite.db' AS sde (TYPE SQLITE);")

query = """
WITH item_stats AS (
    SELECT 
        type_id,
        COUNT(*) as days_active,
        MEDIAN(average) as median_price,
        QUANTILE_CONT(average, 0.15) as p15_price,
        QUANTILE_CONT(average, 0.85) as p85_price,
        AVG(volume * average) as avg_daily_isk
    FROM forge_market_history
    GROUP BY type_id
    HAVING days_active >= 20 
       AND avg_daily_isk BETWEEN 300000000 AND 15000000000  -- 300M to 15B ISK/day
)
SELECT 
    mg.marketGroupName,
    COUNT(s.type_id) as items_in_group,
    ROUND(AVG((s.p85_price - s.p15_price) / s.median_price) * 100, 1) as avg_swing_pct,
    ROUND(AVG(s.avg_daily_isk) / 1e9, 2) as avg_daily_isk_b
FROM item_stats s
JOIN sde.invTypes t ON s.type_id = t.typeID
JOIN sde.invMarketGroups mg ON t.marketGroupID = mg.marketGroupID
GROUP BY mg.marketGroupName
HAVING items_in_group >= 3
ORDER BY avg_swing_pct DESC
LIMIT 20;
"""

df = con.execute(query).df()
print("\n=== TOP 20 VOLATILE MARKET GROUPS (DATA-DRIVEN DISCOVERY) ===")
print(df.to_string(index=False))
