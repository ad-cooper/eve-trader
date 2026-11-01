#!/usr/bin/env python3
"""
EVE Online Inventory Advisor
============================
Cross-references your current character assets against the DuckDB ledger history
to provide context-aware sell recommendations (Hold for Target vs. Undercut).
"""

import os
import duckdb
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.esi import fetch_character_assets
from services.market import create_session, fetch_lowest_sell_order

DUCKDB_PATH = "data/eve_analytics.duckdb"

def get_historical_targets(type_ids):
    if not type_ids:
        return {}
    
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    id_str = ",".join(str(tid) for tid in type_ids)
    
    # Fetch the most recent ledger entry for each item we own
    query = f"""
        WITH RankedHistory AS (
            SELECT 
                type_id,
                name,
                desk_type,
                target_buy,
                target_sell,
                snapshot_time,
                ROW_NUMBER() OVER(PARTITION BY type_id ORDER BY snapshot_time DESC) as rn
            FROM ledger_history
            WHERE type_id IN ({id_str})
        )
        SELECT type_id, name, desk_type, target_buy, target_sell 
        FROM RankedHistory 
        WHERE rn = 1;
    """
    
    try:
        results = con.execute(query).df()
        con.close()
        return {
            row["type_id"]: {
                "name": row["name"],
                "desk": row["desk_type"].upper(),
                "target_buy": row["target_buy"],
                "target_sell": row["target_sell"]
            }
            for _, row in results.iterrows()
        }
    except Exception as e:
        print(f"[!] DuckDB Error: {e}")
        con.close()
        return {}

def main():
    print("[*] Fetching current character assets from ESI...")
    assets = fetch_character_assets()
    if not assets:
        print("[!] No assets found or ESI token expired.")
        return

    JITA_STATION_ID = 60003760

    # 1. Identify all container/ship IDs in Jita / trade hubs
    container_ids = set()
    for a in assets:
        if a.get("location_type") == "item":
            container_ids.add(a.get("location_id"))
        loc = a.get("location_id")
        if (loc == JITA_STATION_ID or (loc and loc >= 1_000_000_000_000)) and a.get("is_singleton"):
            container_ids.add(a.get("item_id"))

    # Aggregate inventory quantities (ignoring containers & items inside containers)
    inventory = {}
    for a in assets:
        loc = a.get("location_id")
        loc_type = a.get("location_type")
        loc_flag = a.get("location_flag")

        # Must be in Jita 4-4 or Forge Citadel
        is_hub = (loc == JITA_STATION_ID) or (loc is not None and loc >= 1_000_000_000_000)
        if not is_hub:
            continue

        # Must be directly in Hangar
        if loc_flag != "Hangar" or loc_type not in ["station", "other"]:
            continue

        # Ignore items in containers or singletons
        if loc_type == "item" or loc in container_ids or a.get("is_singleton"):
            continue

        tid = a["type_id"]
        inventory[tid] = inventory.get(tid, 0) + a.get("quantity", 1)

    print(f"[*] Found {len(inventory)} unique item types in hangar. Looking up historical ledger targets...")
    history_map = get_historical_targets(list(inventory.keys()))
    
    # Filter to only items that were managed by our ledgers
    managed_items = {tid: qty for tid, qty in inventory.items() if tid in history_map}
    
    if not managed_items:
        print("[*] None of your current hangar items match historical ledger tracking.")
        return

    print(f"[*] Fetching live Jita 4-4 order books for {len(managed_items)} managed items...")
    session = create_session()
    live_asks = {}
    
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_lowest_sell_order, session, tid): tid for tid in managed_items.keys()}
        for fut in as_completed(futures):
            tid, lowest_ask = fut.result()
            live_asks[tid] = lowest_ask

    print("\n" + "=" * 135)
    print(f"--- INVENTORY SELL ADVISOR ---")
    print("=" * 135)
    print(f"{'ITEM NAME':<35} | {'DESK':<10} | {'QTY':<5} | {'TARGET SELL':<12} | {'LIVE ASK':<12} | {'ACTION':<25} | {'LIST PRICE'}")
    print("=" * 135)

    for tid, qty in managed_items.items():
        meta = history_map[tid]
        live_ask = live_asks.get(tid)
        t_sell = meta["target_sell"]
        name = meta["name"]
        desk = meta["desk"]

        # Default fallback if item is stocked out in Jita
        if not live_ask:
            live_ask = t_sell
            live_ask_str = "STOCKOUT"
        else:
            live_ask_str = f"{live_ask/1e6:.1f}M" if live_ask >= 1e6 else f"{live_ask:,.0f}"

        t_sell_str = f"{t_sell/1e6:.1f}M" if t_sell >= 1e6 else f"{t_sell:,.0f}"

        # --- CONTEXT-AWARE PRICING LOGIC ---
        if desk == "EVERGREEN":
            # Evergreen prioritizes volume and velocity over holding for a target
            action = "FAST FLIP (Undercut)"
            list_price = live_ask - 0.01
        else:
            # Tactical and Hold prioritize the structural target sell price
            if live_ask >= t_sell:
                action = "TARGET MET (Undercut)"
                list_price = live_ask - 0.01
            else:
                action = "HOLD CYCLE (List @ Target)"
                list_price = t_sell

        list_str = f"{list_price/1e6:.1f}M" if list_price >= 1e6 else f"{list_price:,.0f}"
        
        # Color coding for terminal output (Green for met/undercut, Yellow for hold)
        if "HOLD" in action:
            action_fmt = f"\033[93m{action:<25}\033[0m" # Yellow
        else:
            action_fmt = f"\033[92m{action:<25}\033[0m" # Green

        print(f"{name[:34]:<35} | {desk:<10} | {qty:<5} | {t_sell_str:>12} | {live_ask_str:>12} | {action_fmt} | {list_str:>10}")

    print("=" * 135)

if __name__ == "__main__":
    main()
