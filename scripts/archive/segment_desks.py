import duckdb
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")

# 1. Classify sales into the 3 distinct operational desks
con.execute("""
CREATE OR REPLACE VIEW categorized_sales AS
SELECT 
    tx_date,
    trade_date,
    item_name,
    group_name,
    qty,
    sell_price,
    original_buy_price,
    net_profit_isk,
    profit_pct,
    station_name,
    region_name,
    CASE 
        -- DESK 1: Nullsec Ship In-Station Arbitrage (UALX-3 / C-J6MT)
        WHEN (station_name LIKE '%UALX%' OR station_name LIKE '%C-J6%' OR region_name IN ('Insmother', 'Tenerifis'))
             AND group_name IN ('Cruiser', 'Battleship', 'Industrial Command Ship', 'Exhumer', 
                                'Marauder', 'Black Ops', 'Mining Barge', 'Strategic Cruiser', 
                                'Combat Battlecruiser', 'Capital Industrial Ship', 'Frigate', 'Amarr', 'Minmatar')
        THEN 'DESK 1: Nullsec Ship Station Flip'

        -- DESK 3: Rig Operations (T1/T2 Manufacturing & Imports)
        WHEN group_name LIKE '%Rig%'
        THEN 'DESK 3: Rig Operations'

        -- DESK 2: Faction Module & Small Item Arbitrage (Jita -> C-J6MT / Local Flips)
        ELSE 'DESK 2: Small Item / Faction Arbitrage'
    END as trading_desk
FROM clean_sales;
""")

pd.set_option('display.max_columns', 15)
pd.set_option('display.width', 1000)

# ==============================================================================
# DESK PERFORMANCE SUMMARY
# ==============================================================================
print("\n========================= OVERALL DESK PERFORMANCE =========================")
df_summary = con.execute("""
    SELECT 
        trading_desk,
        COUNT(*) as total_sales,
        ROUND(SUM(net_profit_isk) / 1e9, 2) as total_profit_b,
        ROUND(AVG(profit_pct), 1) as avg_margin_pct,
        ROUND(SUM(CASE WHEN net_profit_isk > 0 THEN net_profit_isk ELSE 0 END) / 1e9, 2) as gross_gains_b,
        ROUND(SUM(CASE WHEN net_profit_isk < 0 THEN net_profit_isk ELSE 0 END) / 1e6, 1) as gross_losses_m,
        ROUND(COUNT(CASE WHEN net_profit_isk > 0 THEN 1 END) * 100.0 / COUNT(*), 1) as win_rate_pct
    FROM categorized_sales
    GROUP BY trading_desk
    ORDER BY total_profit_b DESC;
""").df()
print(df_summary.to_string(index=False))

# ==============================================================================
# DEEP DIVE: DESK 1 (NULLSEC SHIP STATION TRADING)
# ==============================================================================
print("\n================== DESK 1: TOP WINNERS & LOSERS (SHIP FLIPS) ================")
df_desk1 = con.execute("""
    SELECT 
        item_name,
        COUNT(*) as sales_count,
        SUM(qty) as hulls_sold,
        ROUND(SUM(net_profit_isk) / 1e9, 2) as net_profit_b,
        ROUND(AVG(profit_pct), 1) as avg_margin_pct,
        ROUND(AVG(sell_price) / 1e6, 1) as avg_sell_m,
        ROUND(AVG(original_buy_price) / 1e6, 1) as avg_buy_m
    FROM categorized_sales
    WHERE trading_desk = 'DESK 1: Nullsec Ship Station Flip'
    GROUP BY item_name
    ORDER BY net_profit_b DESC;
""").df()
print(df_desk1.to_string(index=False))

# ==============================================================================
# DEEP DIVE: DESK 3 (RIG MANUFACTURING & SPREADS)
# ==============================================================================
print("\n=================== DESK 3: RIG PERFORMANCE BREAKDOWN =======================")
df_desk3 = con.execute("""
    SELECT 
        item_name,
        COUNT(*) as sales_count,
        SUM(qty) as units_sold,
        ROUND(SUM(net_profit_isk) / 1e6, 1) as net_profit_m,
        ROUND(AVG(profit_pct), 1) as avg_margin_pct,
        ROUND(AVG(sell_price) / 1e6, 2) as avg_sell_m,
        ROUND(AVG(original_buy_price) / 1e6, 2) as avg_buy_m,
        station_name
    FROM categorized_sales
    WHERE trading_desk = 'DESK 3: Rig Operations'
    GROUP BY item_name, station_name
    ORDER BY net_profit_m DESC
    LIMIT 20;
""").df()
print(df_desk3.to_string(index=False))
