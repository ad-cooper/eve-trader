import duckdb
import numpy as np
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH 'data/latest-sqlite.db' AS sde (TYPE SQLITE);")

def get_half_life(prices):
    if len(prices) < 20 or np.all(prices == prices[0]):
        return 999.0
    lagged = prices[:-1]
    delta = np.diff(prices)
    slope, _ = np.polyfit(lagged, delta, 1)
    if slope >= 0:
        return 999.0
    return np.log(2) / (-slope)

# Pull items with high ticket value and reasonable daily ISK turnover
candidates = con.execute("""
    SELECT 
        type_id,
        AVG(volume * average) as daily_isk,
        MEDIAN(average) as median_price,
        QUANTILE_CONT(average, 0.15) as p15,
        QUANTILE_CONT(average, 0.85) as p85
    FROM forge_market_history
    GROUP BY type_id
    HAVING COUNT(*) >= 25 
       AND MEDIAN(average) BETWEEN 15000000 AND 800000000  -- 15M to 800M unit price
       AND AVG(volume * average) BETWEEN 300000000 AND 10000000000 -- 300M to 10B ISK/day
""").fetchall()

results = []
print(f"Testing half-life on {len(candidates)} high-density candidates...")

for row in candidates:
    type_id, daily_isk, median_p, p15, p85 = row
    spread_pct = (p85 - p15) / median_p * 100
    
    if spread_pct < 25.0:
        continue

    price_rows = con.execute(f"""
        SELECT average FROM forge_market_history 
        WHERE type_id = {type_id} ORDER BY date ASC
    """).fetchall()
    
    prices = np.array([p[0] for p in price_rows])
    hl = get_half_life(prices)

    # Filter strictly for the 1-check-a-day sweet spot (2.0 to 7.0 days)
    if 2.0 <= hl <= 7.0:
        name = con.execute(f"SELECT typeName FROM sde.invTypes WHERE typeID = {type_id}").fetchone()[0]
        results.append({
            "item_name": name,
            "half_life_days": round(hl, 1),
            "median_price_m": round(median_p / 1e6, 1),
            "p15_buy_m": round(p15 / 1e6, 1),
            "p85_sell_m": round(p85 / 1e6, 1),
            "spread_pct": round(spread_pct, 1),
            "daily_isk_b": round(daily_isk / 1e9, 2)
        })

df = pd.DataFrame(results)
if not df.empty:
    df.sort_values(by="spread_pct", ascending=False, inplace=True)
    pd.set_option('display.max_rows', 50)
    pd.set_option('display.width', 1000)
    print("\n=== THE ONCE-A-DAY SWEET SPOT (2 to 7 Day Half-Life | >15M ISK Unit Price) ===")
    print(df.to_string(index=False))
else:
    print("No items matched all constraints.")
