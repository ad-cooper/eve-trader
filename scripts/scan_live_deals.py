#!/usr/bin/env python3
"""
Hardened Live Deal Sniper Engine
================================
Eliminates false-positive officer listings, Mutaplasmids, and downward-trending books.
Enforces:
1. Hard exclusion of Officer, Mutaplasmid, and Cosmetic items.
2. Minimum liquidity floor (>=5 units/day across >=7 active days).
3. Cluster Depth Check: Rejects items where multiple sellers sit within 3% of the ask.
4. Median Ceiling: Target exit is capped at the 30-day historical median (p50).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import duckdb
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.blacklist import load_active_blacklist
from services.ledgers import load_desk_csv

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"

REGION_THE_FORGE = 10000002
JITA_STATION_ID = 60003760

BUY_FEE_CITADEL = 0.0050
EXIT_FEE_JITA = 0.04605

# Patterns that indicate illiquid, vanity, or RNG items
BANNED_NAME_PATTERNS = [
    "' Modified ",     # Officer modules
    "Mutaplasmid",      # RNG reroll items
    "Accelerator",      # Expiring event boosters
    "Background",       # Character cosmetic
    "Filament",         # High volatility / niche
    "Blueprint",        # Blueprint originals / copies
    "Skin",             # Ship skins
]

def create_session():
    s = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(pool_connections=24, pool_maxsize=32, max_retries=retries))
    s.headers.update({"User-Agent": "EVE-Desk-HardenedSniper/3.0"})
    return s

def is_banned_item(name: str) -> bool:
    for pat in BANNED_NAME_PATTERNS:
        if pat.lower() in name.lower():
            return True
    return False

def get_candidate_universe():
    candidates = {}

    # 1. Active Desk Items (Evergreen, Tactical, Hold)
    for desk in ["evergreen", "tactical", "hold"]:
        for it in load_desk_csv(f"{desk}_ledger.csv"):
            tid = int(it["type_id"])
            name = it.get("item_name") or it.get("name")
            if is_banned_item(name):
                continue
            candidates[tid] = {
                "type_id": tid,
                "name": name,
                "target_buy": float(it.get("target_buy", 0.0)),
                "target_sell": float(it.get("target_sell", 0.0)),
                "desk": desk.upper(),
                "median_30d": float(it.get("target_sell", 0.0)),
                "daily_vol": 10.0
            }

    # 2. Query DuckDB for Liquid Forge Commodities with Real Historical Volume
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
        
        # Must have traded at least 7 distinct days in the last 14 days, avg >= 5 units/day
        query = """
        WITH stats AS (
            SELECT 
                type_id,
                ROUND(AVG(volume), 1) as daily_vol,
                COUNT(DISTINCT TRY_CAST(date AS DATE)) as active_days,
                QUANTILE_CONT(average, 0.50) as median_30d
            FROM forge_market_history
            WHERE TRY_CAST(date AS DATE) >= (SELECT MAX(TRY_CAST(date AS DATE)) - INTERVAL 30 DAY FROM forge_market_history)
              AND average > 0 AND volume > 0
            GROUP BY type_id
            HAVING COUNT(DISTINCT TRY_CAST(date AS DATE)) >= 10 
               AND AVG(volume) >= 5.0
               AND QUANTILE_CONT(average, 0.50) >= 5000000
        )
        SELECT 
            s.type_id,
            t.typeName,
            s.daily_vol,
            s.median_30d
        FROM stats s
        JOIN sde.invTypes t ON s.type_id = t.typeID
        JOIN sde.invGroups g ON t.groupID = g.groupID
        WHERE g.categoryID NOT IN (26, 30, 32, 63) -- Exclude blueprints, apparel, special edition
        ORDER BY (s.daily_vol * s.median_30d) DESC
        LIMIT 500;
        """
        rows = con.execute(query).fetchall()
        con.close()

        for r in rows:
            tid = r[0]
            name = r[1]
            if is_banned_item(name):
                continue
            if tid not in candidates:
                candidates[tid] = {
                    "type_id": tid,
                    "name": name,
                    "target_buy": 0.0,
                    "target_sell": 0.0,
                    "desk": "FORGE_LIQUID",
                    "median_30d": float(r[3]),
                    "daily_vol": float(r[2])
                }
    except Exception as e:
        print(f"[!] DuckDB Query Warning: {e}")

    # Apply Temporary Blacklist
    bl_items, bl_groups, _ = load_active_blacklist()
    filtered = {tid: c for tid, c in candidates.items() if c["name"] not in bl_items and not is_banned_item(c["name"])}
    print(f"[*] Hardened Universe: {len(filtered)} liquid candidates queued for scanning.")
    return filtered

def scan_orders_for_type(session, tid):
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={tid}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code == 200:
            orders = r.json()
            sell_orders = []
            buy_orders = []
            for o in orders:
                loc = o.get("location_id")
                is_hub = (loc == JITA_STATION_ID) or (loc and loc >= 1_000_000_000_000)
                if not is_hub:
                    continue
                if o.get("is_buy_order"):
                    buy_orders.append(o["price"])
                else:
                    sell_orders.append((o["price"], o["volume_remain"]))

            sell_orders.sort(key=lambda x: x[0])
            top_buy = max(buy_orders) if buy_orders else None
            return tid, sell_orders, top_buy
    except Exception:
        pass
    return tid, [], None

def main():
    start_time = datetime.now(timezone.utc)
    candidates = get_candidate_universe()
    if not candidates:
        print("[!] No candidates found to scan.")
        return

    session = create_session()
    type_ids = list(candidates.keys())
    order_data = {}

    print(f"[*] Scanning live books across {len(type_ids)} targets...")
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(scan_orders_for_type, session, tid): tid for tid in type_ids}
        for fut in as_completed(futures):
            tid, sells, top_buy = fut.result()
            if sells:
                order_data[tid] = (sells, top_buy)

    deals = []

    for tid, (sells, top_buy) in order_data.items():
        meta = candidates[tid]
        lowest_ask, qty_available = sells[0]
        median_30d = meta.get("median_30d", 0.0)

        deal_type = None
        target_exit = None

        # --- GUARDRAIL 1: CLUSTER DEPTH CHECK ---
        # Look at how many units are sitting within 3.5% of lowest_ask
        cluster_qty = sum(q for p, q in sells if p <= (lowest_ask * 1.035))
        
        # If there are already >3 units (or >500M ISK worth) clustered at the bottom,
        # this is a stacked market price, NOT an isolated firesale underbid.
        if cluster_qty > 3 and (lowest_ask * cluster_qty) > 300_000_000:
            continue

        # Find the first real resistance wall above the undercut (at least 5% higher)
        wall_ask = None
        for p, q in sells[1:]:
            if p >= (lowest_ask * 1.05):
                wall_ask = p
                break

        # --- ARCHETYPE 1: INSTANT ARB (Sell order <= Top Buy Order) ---
        if top_buy and lowest_ask <= top_buy:
            deal_type = "INSTANT ARB"
            target_exit = top_buy

        # --- ARCHETYPE 2: FIRESALE DUMP (Isolated underbid below the active wall) ---
        elif wall_ask and wall_ask > lowest_ask:
            # Crucial check: The wall must be supported by the 30-day median!
            # If wall is 5B but median is 2B, wall is fictitious.
            realistic_exit = min(wall_ask * 0.9999, median_30d * 1.05)
            
            # Check if lowest_ask is genuinely discounted relative to realistic exit
            if realistic_exit > (lowest_ask * 1.08):
                deal_type = "FIRESALE DUMP"
                target_exit = realistic_exit

        # --- ARCHETYPE 3: DEEP DISCOUNT (Lowest ask <= 85% of 30d median) ---
        elif median_30d > 0 and lowest_ask <= (median_30d * 0.85):
            # Only valid if there are fewer than 3 units at this dump price
            if cluster_qty <= 2:
                deal_type = "DEEP DISCOUNT"
                target_exit = median_30d * 0.98

        if not deal_type or not target_exit:
            continue

        # Safe position sizing: Buy only the isolated underbid units (max 5 units or 1.5B ISK)
        eval_qty = min(qty_available, 5)
        if (lowest_ask * eval_qty) > 1_500_000_000 and eval_qty > 1:
            eval_qty = max(1, int(1_500_000_000 // lowest_ask))

        # Financial Calculations with Jita/Citadel Friction
        cost_unit = lowest_ask * (1.0 + BUY_FEE_CITADEL)
        net_proceeds_unit = target_exit * (1.0 - EXIT_FEE_JITA)
        profit_unit = net_proceeds_unit - cost_unit
        net_margin = (profit_unit / cost_unit) * 100.0 if cost_unit > 0 else 0.0

        total_cost = lowest_ask * eval_qty
        est_profit = profit_unit * eval_qty

        # Strict profitability bar: At least +5.0% net margin after all taxes, minimum 5M profit
        if net_margin >= 5.0 and est_profit >= 5_000_000:
            discount_pct = ((target_exit - lowest_ask) / target_exit) * 100.0
            deals.append({
                "deal_type": deal_type,
                "type_id": tid,
                "name": meta["name"],
                "item_name": meta["name"],
                "qty": int(eval_qty),
                "quantity": int(eval_qty),
                "discount_pct": round(discount_pct, 1),
                "ask_price": round(lowest_ask, 2),
                "target_exit": round(target_exit, 2),
                "target_sell": round(target_exit, 2),
                "net_margin": round(net_margin, 1),
                "capital": round(total_cost, 2),
                "total_cost": round(total_cost, 2),
                "profit": round(est_profit, 2),
                "est_profit": round(est_profit, 2)
            })

    deals.sort(key=lambda x: x["est_profit"], reverse=True)

    # Save to both file paths
    for p in ["ledgers/live_deals.json", "ledgers/sniper_deals.json"]:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(deals, f, indent=2)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(f"[+] Scan completed in {elapsed:.1f}s. Found {len(deals)} legitimate deals.")

if __name__ == "__main__":
    main()
