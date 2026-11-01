import duckdb
import numpy as np
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")

WATCHLIST = [
    31720,  # Capital Hyperspatial Velocity Optimizer II
    31718,  # Capital Hyperspatial Velocity Optimizer I
    31177,  # Medium Bay Loading Accelerator II
    31179,  # Medium Warhead Calefaction Catalyst II
    31794,  # Large Core Defense Field Purger II
    14041,  # Corpum A-Type Thermal Energized Membrane
    14043   # Centum A-Type Thermal Energized Membrane
]

con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH 'data/latest-sqlite.db' AS sde (TYPE SQLITE);")

def get_half_life(prices):
    if len(prices) < 15 or np.all(prices == prices[0]):
        return 999.0
    lagged = prices[:-1]
    delta = np.diff(prices)
    slope, _ = np.polyfit(lagged, delta, 1)
    if slope >= 0:
        return 999.0
    return np.log(2) / (-slope)

results = []
for type_id in WATCHLIST:
    rows = con.execute(f"""
        SELECT average FROM forge_market_history 
        WHERE type_id = {type_id} ORDER BY date ASC
    """).fetchall()
    
    name = con.execute(f"SELECT typeName FROM sde.invTypes WHERE typeID = {type_id}").fetchone()[0]
    prices = np.array([r[0] for r in rows])
    hl = get_half_life(prices)
    
    results.append({
        "item_name": name,
        "half_life_days": round(hl, 1),
        "30d_min": int(np.min(prices)),
        "30d_max": int(np.max(prices))
    })

print("\n=== REVERSION HALF-LIFE (HOLDING DURATION) ===")
print(pd.DataFrame(results).to_string(index=False))
