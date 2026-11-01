import duckdb
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH 'data/latest-sqlite.db' AS sde (TYPE SQLITE);")

print("Ingesting and cleaning transaction_export.csv...")

con.execute("""
CREATE OR REPLACE TABLE raw_transactions AS 
SELECT * FROM read_csv_auto('data/transaction_export.csv', ignore_errors=true);
""")

# Clean and normalize strings, numbers, currencies, and percentages
con.execute("""
CREATE OR REPLACE VIEW clean_sales AS
SELECT 
    CAST("Date" AS TIMESTAMP) as tx_date,
    CAST("Date" AS DATE) as trade_date,
    CAST("Name" AS VARCHAR) as item_name,
    CAST("Group" AS VARCHAR) as group_name,
    ABS(TRY_CAST(REPLACE(REPLACE(CAST("Quantity" AS VARCHAR), ',', ''), ' ', '') AS BIGINT)) as qty,
    TRY_CAST(REPLACE(REPLACE(REPLACE(REPLACE(CAST("Price" AS VARCHAR), ' ISK', ''), '€', ''), ',', ''), ' ', '') AS DOUBLE) as sell_price,
    TRY_CAST(REPLACE(REPLACE(REPLACE(REPLACE(CAST("Transaction Price" AS VARCHAR), ' ISK', ''), '€', ''), ',', ''), ' ', '') AS DOUBLE) as original_buy_price,
    TRY_CAST(REPLACE(REPLACE(REPLACE(REPLACE(CAST("Transaction Profit +" AS VARCHAR), ' ISK', ''), '€', ''), ',', ''), ' ', '') AS DOUBLE) as net_profit_isk,
    TRY_CAST(REPLACE(REPLACE(REPLACE(CAST("Transaction Profit %" AS VARCHAR), '%', ''), ',', ''), ' ', '') AS DOUBLE) as profit_pct,
    CAST("Station" AS VARCHAR) as station_name,
    CAST("Region" AS VARCHAR) as region_name,
    UPPER(CAST("Type" AS VARCHAR)) as tx_type
FROM raw_transactions
WHERE UPPER(CAST("Type" AS VARCHAR)) LIKE '%SELL%'
  AND "Transaction Profit +" IS NOT NULL;
""")

pd.set_option('display.max_columns', 15)
pd.set_option('display.width', 1000)

# ==============================================================================
# REPORT 1: WHAT WORKED (TOP PROFIT ENGINES)
# ==============================================================================
print("\n==================== 1. WHAT WORKED (TOP PROFIT DRIVERS) ====================")
df_winners = con.execute("""
    SELECT 
        item_name,
        group_name,
        COUNT(*) as total_sales,
        SUM(qty) as units_sold,
        ROUND(SUM(net_profit_isk) / 1e9, 2) as total_profit_b,
        ROUND(AVG(sell_price) / 1e6, 2) as avg_sell_price_m,
        ROUND(AVG(original_buy_price) / 1e6, 2) as avg_buy_price_m,
        ROUND(AVG(profit_pct), 1) as avg_margin_pct
    FROM clean_sales
    WHERE net_profit_isk > 0
    GROUP BY item_name, group_name
    ORDER BY total_profit_b DESC
    LIMIT 20;
""").df()
print(df_winners.to_string(index=False) if not df_winners.empty else "No completed sales with profit data found.")

# ==============================================================================
# REPORT 2: WHAT DIDN'T WORK (LOSSES & UNDERPERFORMERS)
# ==============================================================================
print("\n================ 2. WHAT DIDN'T WORK (LOSSES & CAPITAL LEAKS) ================")
df_losers = con.execute("""
    SELECT 
        item_name,
        group_name,
        COUNT(*) as total_sales,
        SUM(qty) as units_sold,
        ROUND(SUM(net_profit_isk) / 1e6, 1) as total_loss_m,
        ROUND(AVG(sell_price) / 1e6, 2) as avg_sell_price_m,
        ROUND(AVG(original_buy_price) / 1e6, 2) as avg_buy_price_m,
        ROUND(AVG(profit_pct), 1) as avg_margin_pct
    FROM clean_sales
    WHERE net_profit_isk < 0
    GROUP BY item_name, group_name
    ORDER BY total_loss_m ASC
    LIMIT 20;
""").df()
print(df_losers.to_string(index=False) if not df_losers.empty else "No losing trades found in the export.")

# ==============================================================================
# REPORT 3: THE AUTOPSY (CROSS-REFERENCE WITH JITA BENCHMARKS)
# ==============================================================================
print("\n================== 3. THE AUTOPSY: DIAGNOSING WHY LOSSES OCCURRED ============")
df_autopsy = con.execute("""
    WITH loser_samples AS (
        SELECT 
            s.item_name,
            t.typeID as type_id,
            s.trade_date,
            s.qty,
            s.original_buy_price,
            s.sell_price,
            s.net_profit_isk,
            s.profit_pct,
            s.region_name
        FROM clean_sales s
        JOIN sde.invTypes t ON s.item_name = t.typeName
        WHERE s.net_profit_isk < -5000000 -- Losses worse than -5M ISK
    )
    SELECT 
        l.item_name,
        l.trade_date as sale_date,
        l.qty,
        ROUND(l.original_buy_price / 1e6, 2) as your_buy_m,
        ROUND(l.sell_price / 1e6, 2) as your_sell_m,
        ROUND(jita.average / 1e6, 2) as jita_avg_on_sale_date_m,
        -- Check if sale price was undercutting Jita itself
        ROUND((l.sell_price - jita.average) / NULLIF(jita.average, 0) * 100, 1) as sell_vs_jita_pct,
        ROUND(l.net_profit_isk / 1e6, 1) as net_loss_m,
        l.region_name
    FROM loser_samples l
    LEFT JOIN forge_market_history jita ON l.type_id = jita.type_id AND l.trade_date = jita.date
    ORDER BY l.net_profit_isk ASC
    LIMIT 20;
""").df()
print(df_autopsy.to_string(index=False) if not df_autopsy.empty else "No autopsy candidates found.")
