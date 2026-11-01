import duckdb
import numpy as np
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH 'data/latest-sqlite.db' AS sde (TYPE SQLITE);")

print("Scanning 3.49M records for structurally stable swing items...")

# 1. Pull candidates with 300+ trading days, solid ticket size, and healthy daily volume
candidates = con.execute("""
    SELECT 
        type_id,
        COUNT(*) as days_active,
        AVG(volume * average) as avg_daily_isk,
        MEDIAN(average) as median_p,
        QUANTILE_CONT(average, 0.10) as p10_floor,
        QUANTILE_CONT(average, 0.90) as p90_ceiling
    FROM forge_market_history
    GROUP BY type_id
    HAVING days_active >= 300
       AND MEDIAN(average) BETWEEN 15000000 AND 600000000        -- 15M to 600M unit value
       AND AVG(volume * average) BETWEEN 300000000 AND 8000000000 -- 300M to 8B/day turnover
""").fetchall()

print(f"Analyzing multi-year price trajectory across {len(candidates)} candidates...")

stable_items = []

for row in candidates:
    type_id, days, daily_isk, median_p, p10, p90 = row
    
    # Require at least a 35% envelope between P10 and P90
    spread_pct = (p90 - p10) / median_p * 100
    if spread_pct < 35.0:
        continue

    # Fetch daily price series chronologically
    price_rows = con.execute(f"""
        SELECT average FROM forge_market_history 
        WHERE type_id = {type_id} ORDER BY date ASC
    """).fetchall()
    
    prices = np.array([p[0] for p in price_rows])
    days_count = len(prices)
    if days_count < 2:
        continue

    # Calculate 365-day annual drift slope (linear regression)
    x = np.arange(days_count)
    slope, intercept = np.polyfit(x, prices, 1)
    annual_drift_pct = (slope * days_count) / median_p * 100

    # DRIFT FILTER: Reject items in a secular downward spiral
    if annual_drift_pct < -5.0:
        continue

    # MAX DRAWDOWN CHECK: Ensure recent price is not in freefall below historical floor
    recent_30d_median = np.median(prices[-30:])
    if recent_30d_median < (p10 * 0.90):
        continue

    # Lookup Name and Market Group
    info = con.execute(f"""
        SELECT t.typeName, mg.marketGroupName 
        FROM sde.invTypes t
        LEFT JOIN sde.invMarketGroups mg ON t.marketGroupID = mg.marketGroupID
        WHERE t.typeID = {type_id}
    """).fetchone()
    
    item_name = info[0] if info else f"Type {type_id}"
    group_name = info[1] if info and info[1] else "General"

    # Exclude SKINR paints, cosmetic pigments, boosters, and RNG mutaplasmids
    if any(tag in item_name for tag in ['Metallic', 'Limited', 'Booster', 'SKIN', 'Mutaplasmid', 'Certificate']):
        continue

    stable_items.append({
        "item_name": item_name,
        "group": group_name,
        "p10_buy_floor": int(round(p10, 0)),
        "p90_sell_target": int(round(p90, 0)),
        "spread_pct": round(spread_pct, 1),
        "annual_drift_pct": round(annual_drift_pct, 1),
        "daily_isk_b": round(daily_isk / 1e9, 2)
    })

df = pd.DataFrame(stable_items)
if not df.empty:
    df.sort_values(by="spread_pct", ascending=False, inplace=True)
    pd.set_option('display.max_rows', 40)
    pd.set_option('display.width', 1000)
    print(f"\nFound {len(df)} Structurally Stable 90-Day Candidates:")
    print(df.to_string(index=False))
else:
    print("No items met all structural criteria.")
