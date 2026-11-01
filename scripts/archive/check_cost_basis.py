import duckdb
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")

# List of items from your liquidation batch
target_items = [
    'Logic Circuit',
    'Enhanced Ward Console',
    'Nanite Compound',
    'Intact Shield Emitter',
    'Power Circuit',
    'Impetus Console',
    'Capacitor Console',
    'Single-crystal Superalloy I-beam',
    'Trigger Unit',
    'Micro Circuit',
    'Interface Circuit',
    'Power Conduit',
    'Oxygen Isotopes',
    'Nitrogen Isotopes',
    'Datacore - Molecular Engineering',
    'Datacore - Hydromagnetic Physics',
    'Datacore - Nanite Engineering',
    'Datacore - Quantum Physics',
    'Datacore - Electromagnetic Physics'
]

# Query historical buy transactions
query = f"""
WITH cleaned_buys AS (
    SELECT 
        CAST("Date" AS TIMESTAMP) as buy_date,
        CAST("Name" AS VARCHAR) as item_name,
        ABS(TRY_CAST(REPLACE(REPLACE(CAST("Quantity" AS VARCHAR), ',', ''), ' ', '') AS BIGINT)) as qty,
        TRY_CAST(REPLACE(REPLACE(REPLACE(REPLACE(CAST("Price" AS VARCHAR), ' ISK', ''), '€', ''), ',', ''), ' ', '') AS DOUBLE) as unit_price
    FROM raw_transactions
    WHERE UPPER(CAST("Type" AS VARCHAR)) LIKE '%BUY%'
      AND "Name" IN ({','.join([f"'{x}'" for x in target_items])})
)
SELECT 
    item_name,
    COUNT(*) as buy_transactions,
    SUM(qty) as total_units_bought,
    ROUND(SUM(qty * unit_price) / NULLIF(SUM(qty), 0), 2) as weighted_avg_buy_price,
    ROUND(MIN(unit_price), 2) as min_buy_price,
    ROUND(MAX(unit_price), 2) as max_buy_price,
    MAX(buy_date)::DATE as last_buy_date
FROM cleaned_buys
GROUP BY item_name
ORDER BY SUM(qty * unit_price) DESC;
"""

df = con.execute(query).df()
pd.set_option('display.max_columns', 10)
pd.set_option('display.width', 1000)

print("\n==================== HISTORICAL PURCHASE COST BASIS ====================")
if not df.empty:
    print(df.to_string(index=False))
else:
    print("No matching historical buy transactions found in transaction_export.csv.")
print("========================================================================")
