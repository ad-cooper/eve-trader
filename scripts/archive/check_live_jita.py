import sys
import requests
import duckdb
import pandas as pd

# 1. Connect to local SDE to resolve item names to typeIDs
con = duckdb.connect("data/eve_analytics.duckdb")
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH 'data/latest-sqlite.db' AS sde (TYPE SQLITE);")

JITA_STATION_ID = 60003760
REGION_ID = 10000002
HEADERS = {"User-Agent": "EveTraderAnalytics/1.0 (Contact: local-trading-script)"}

def get_type_info(name):
    res = con.execute("""
        SELECT typeID, typeName 
        FROM sde.invTypes 
        WHERE LOWER(typeName) = LOWER(?)
    """, [name.strip()]).fetchone()
    return res

def fetch_jita_orders(type_id):
    url = f"https://esi.evetech.net/latest/markets/{REGION_ID}/orders/"
    params = {
        "datasource": "tranquility",
        "order_type": "all",
        "type_id": type_id
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        orders = r.json()
    except Exception as e:
        print(f"ESI Error: {e}")
        return [], []

    # Filter strictly for Jita 4-4 Navy Assembly Plant
    jita_orders = [o for o in orders if o.get("location_id") == JITA_STATION_ID]
    
    buys = sorted([o for o in jita_orders if o.get("is_buy_order")], key=lambda x: x["price"], reverse=True)
    sells = sorted([o for o in jita_orders if not o.get("is_buy_order")], key=lambda x: x["price"])
    
    return buys, sells

def analyze_item(name, target_volume=None):
    info = get_type_info(name)
    if not info:
        print(f"Error: '{name}' not found in SDE database.")
        return

    type_id, type_name = info
    buys, sells = fetch_jita_orders(type_id)

    if not sells and not buys:
        print(f"\n[{type_name}] No active orders found in Jita 4-4.")
        return

    best_buy = buys[0]["price"] if buys else 0.0
    best_sell = sells[0]["price"] if sells else 0.0
    spread_pct = ((best_sell - best_buy) / best_buy * 100) if best_buy > 0 else 0.0

    print(f"\n=======================================================")
    print(f" LIVE JITA 4-4: {type_name} (TypeID: {type_id})")
    print(f"=======================================================")
    print(f" Top Buy Price : {best_buy:,.2f} ISK ({buys[0]['volume_remain']:,} units)")
    print(f" Top Sell Price: {best_sell:,.2f} ISK ({sells[0]['volume_remain']:,} units)")
    print(f" Bid/Ask Spread: {spread_pct:.2f}%")
    print(f" Recommended Listing Price: {best_sell - 0.01:,.2f} ISK (Undercut by 0.01)")

    # Buy Order Depth (How much can be instant-dumped before price drops)
    if target_volume:
        vol_needed = target_volume
        total_payout = 0.0
        slippage_tiers = []
        for b in buys:
            fill = min(vol_needed, b["volume_remain"])
            total_payout += fill * b["price"]
            vol_needed -= fill
            slippage_tiers.append((b["price"], fill))
            if vol_needed <= 0:
                break
        
        effective_avg = total_payout / (target_volume - vol_needed) if (target_volume - vol_needed) > 0 else 0.0
        pct_loss = ((best_buy - effective_avg) / best_buy * 100) if best_buy > 0 else 0.0
        
        print(f"\n--- Instant-Sell Depth Audit for {target_volume:,} units ---")
        if vol_needed > 0:
            print(f" WARNING: Buy orders only cover {target_volume - vol_needed:,} of {target_volume:,} units!")
        print(f" Effective Net Realized Price: {effective_avg:,.2f} ISK")
        print(f" Immediate Slippage Penalty  : -{pct_loss:.2f}% vs Top Buy Order")

    print("\nTop 3 Sell Orders:")
    for s in sells[:3]:
        print(f"  - {s['price']:>12,.2f} ISK | Vol: {s['volume_remain']:>6,}")

    print("Top 3 Buy Orders:")
    for b in buys[:3]:
        print(f"  - {b['price']:>12,.2f} ISK | Vol: {b['volume_remain']:>6,}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        item_input = sys.argv[1]
        qty_input = int(sys.argv[2]) if len(sys.argv) > 2 else None
        analyze_item(item_input, qty_input)
    else:
        # Default test batch from your liquidation list
        test_items = [
            ("Logic Circuit", 13443),
            ("Enhanced Ward Console", 8690),
            ("Oxygen Isotopes", 16917979)
        ]
        for item, qty in test_items:
            analyze_item(item, qty)
