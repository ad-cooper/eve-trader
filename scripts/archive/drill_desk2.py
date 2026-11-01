import duckdb
import pandas as pd

con = duckdb.connect("data/eve_analytics.duckdb")

pd.set_option('display.max_columns', 15)
pd.set_option('display.width', 1000)

print("\n================== DESK 2: TOP 20 PROFIT DRIVERS ==================")
df_desk2_winners = con.execute("""
    SELECT 
        item_name,
        group_name,
        COUNT(*) as sales_count,
        SUM(qty) as units_sold,
        ROUND(SUM(net_profit_isk) / 1e9, 2) as net_profit_b,
        ROUND(AVG(profit_pct), 1) as avg_margin_pct,
        ROUND(AVG(sell_price) / 1e6, 2) as avg_sell_m,
        ROUND(AVG(original_buy_price) / 1e6, 2) as avg_buy_m,
        station_name
    FROM categorized_sales
    WHERE trading_desk = 'DESK 2: Small Item / Faction Arbitrage'
      AND net_profit_isk > 0
    GROUP BY item_name, group_name, station_name
    ORDER BY net_profit_b DESC
    LIMIT 20;
""").df()
print(df_desk2_winners.to_string(index=False))

print("\n================== DESK 2: TOP 20 LOSS LEAKS ======================")
df_desk2_losers = con.execute("""
    SELECT 
        item_name,
        group_name,
        COUNT(*) as sales_count,
        SUM(qty) as units_sold,
        ROUND(SUM(net_profit_isk) / 1e6, 1) as net_loss_m,
        ROUND(AVG(profit_pct), 1) as avg_margin_pct,
        ROUND(AVG(sell_price) / 1e6, 2) as avg_sell_m,
        ROUND(AVG(original_buy_price) / 1e6, 2) as avg_buy_m,
        station_name
    FROM categorized_sales
    WHERE trading_desk = 'DESK 2: Small Item / Faction Arbitrage'
      AND net_profit_isk < 0
    GROUP BY item_name, group_name, station_name
    ORDER BY net_loss_m ASC
    LIMIT 20;
""").df()
print(df_desk2_losers.to_string(index=False))
