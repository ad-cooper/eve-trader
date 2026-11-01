#!/usr/bin/env python3
"""
High-Coverage Live Deal Sniper Engine
====================================
Scans Jita 4-4 & Perimeter order books across:
1. Desk Ledgers (Evergreen, Tactical, Hold)
2. Top High-Margin Forge Traded Items (Faction/Deadspace/Implants/Ships from DuckDB)

Detects:
- FIRESALE DUMP: Fat-finger or panic undercuts relative to 2nd lowest ask
- INSTANT ARB: Asks priced at or below competing top buy orders
- LEDGER BYPASS: Asks cheaper than your desk's target buy price
- DEEP DISCOUNT: Asks >12% below 7d VWAP on liquid items
"""

import os
import sys

# Ensure project root is available for imports
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
OUTPUT_FILE = "ledgers/live_deals.json"

REGION_THE_FORGE = 10000002
JITA_STATION_ID = 60003760

BUY_FEE_CITADEL = 0.0050
EXIT_FEE_JITA = 0.04605

def create_session():
    s = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(pool_connections=24, pool_maxsize=32, max_retries=retries))
    s.headers.update({"User-Agent": "EVE-Desk-SniperEngine/2.0"})
    return s

def get_candidate_universe():
    """
    Builds a robust candidate list:
    - All items currently in your 3 ledgers
    - Top 400 actively traded high-value Forge items from DuckDB
    """
    candidates = {}

    # 1. Active Desk Items
    for desk in ["evergreen", "tactical", "hold"]:
        for it in load_desk_csv(f"{desk}_ledger.csv"):
            tid = int(it["type_id"])
            candidates[tid] = {
                "type_id": tid,
                "name": it.get("item_name") or it.get("name"),
                "target_buy": float(it.get("target_buy", 0.0)),
                "target_sell": float(it.get("target_sell", 0.0)),
                "desk": desk.upper(),
                "vwap_7d": float(it.get("target_sell", 0.0))
            }

    # 2. Top Forge candidates from DuckDB
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
        query = """
        WITH stats AS (
            SELECT 
                type_id,
                AVG(volume) as daily_vol,
                SUM(average * volume) / NULLIF(SUM(volume), 0) as vwap_7d
            FROM forge_market_history
            WHERE TRY_CAST(date AS DATE) >= (SELECT MAX(TRY_CAST(date AS DATE)) - INTERVAL 14 DAY FROM forge_market_history)
              AND average > 0 AND volume > 0
            GROUP BY type_id
            HAVING AVG(volume) >= 3 AND (SUM(average * volume) / NULLIF(SUM(volume), 0)) >= 5000000
        )
        SELECT 
            s.type_id,
            t.typeName,
            s.daily_vol,
            s.vwap_7d
        FROM stats s
        JOIN sde.invTypes t ON s.type_id = t.typeID
        JOIN sde.invGroups g ON t.groupID = g.groupID
        ORDER BY (s.daily_vol * s.vwap_7d) DESC
        LIMIT 400;
        """
        rows = con.execute(query).fetchall()
        con.close()

        for r in rows:
            tid = r[0]
            if tid not in candidates:
                candidates[tid] = {
                    "type_id": tid,
                    "name": r[1],
                    "target_buy": 0.0,
                    "target_sell": 0.0,
                    "desk": "FORGE_TOP",
                    "vwap_7d": float(r[3]) if r[3] else 0.0
                }
    except Exception as e:
        print(f"[!] Warning reading DuckDB: {e}")

    # 3. Apply Blacklist
    bl_items, bl_groups, _ = load_active_blacklist()
    filtered = {tid: c for tid, c in candidates.items() if c["name"] not in bl_items}
    print(f"[*] Sniper Universe: {len(filtered)} items queued for scanning (after blacklist filtering).")
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
                # Jita 4-4 station or Upwell citadel in region
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

    print(f"[*] Querying live Jita/Perimeter order books across {len(type_ids)} targets...")
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
        second_ask = sells[1][0] if len(sells) > 1 else None
        target_buy = meta.get("target_buy", 0.0)
        target_sell = meta.get("target_sell", 0.0)
        vwap_7d = meta.get("vwap_7d", 0.0)

        deal_type = None
        ref_sell = None
        discount_pct = 0.0

        # ARCHETYPE 1: INSTANT ARB (Sell order <= Top Buy Order)
        if top_buy and lowest_ask <= top_buy:
            deal_type = "INSTANT ARB"
            ref_sell = top_buy
            discount_pct = ((lowest_ask - top_buy) / top_buy) * 100

        # ARCHETYPE 2: FIRESALE DUMP (Underbid >= 6% below 2nd lowest ask)
        elif second_ask and second_ask > lowest_ask:
            spread_vs_second = ((second_ask - lowest_ask) / second_ask) * 100
            if spread_vs_second >= 6.0:
                deal_type = "FIRESALE DUMP"
                ref_sell = second_ask
                discount_pct = -spread_vs_second

        # ARCHETYPE 3: LEDGER BYPASS (Ask <= Desk target buy)
        elif target_buy > 0 and lowest_ask <= target_buy:
            deal_type = "LEDGER BYPASS"
            ref_sell = target_sell if target_sell > 0 else (lowest_ask * 1.10)
            discount_pct = ((lowest_ask - target_buy) / target_buy) * 100

        # ARCHETYPE 4: DEEP DISCOUNT (Ask >= 10% below 7d VWAP)
        elif vwap_7d > 0 and lowest_ask <= (vwap_7d * 0.90):
            deal_type = "DEEP DISCOUNT"
            ref_sell = vwap_7d
            discount_pct = ((lowest_ask - vwap_7d) / vwap_7d) * 100

        if not deal_type or not ref_sell:
            continue

        # Cap evaluation quantity for capital risk (max 10 units or up to 1B ISK)
        eval_qty = min(qty_available, 10)
        if (lowest_ask * eval_qty) > 1_500_000_000 and eval_qty > 1:
            eval_qty = max(1, int(1_500_000_000 // lowest_ask))

        # Financial Calculations after friction
        cost_unit = lowest_ask * (1.0 + BUY_FEE_CITADEL)
        net_proceeds_unit = ref_sell * (1.0 - EXIT_FEE_JITA)
        profit_unit = net_proceeds_unit - cost_unit
        net_margin = (profit_unit / cost_unit) * 100.0 if cost_unit > 0 else 0.0

        # Require a minimum positive net margin (at least +3.0%) and 5M ISK profit
        total_cost = lowest_ask * eval_qty
        est_profit = profit_unit * eval_qty

        if net_margin >= 3.0 and est_profit >= 5_000_000:
            deals.append({
                "deal_type": deal_type,
                "type_id": tid,
                "name": meta["name"],
                "item_name": meta["name"],
                "qty": int(eval_qty),
                "quantity": int(eval_qty),
                "discount_pct": round(abs(discount_pct), 1),
                "ask_price": round(lowest_ask, 2),
                "target_exit": round(ref_sell, 2),
                "target_sell": round(ref_sell, 2),
                "net_margin": round(net_margin, 1),
                "capital": round(total_cost, 2),
                "total_cost": round(total_cost, 2),
                "profit": round(est_profit, 2),
                "est_profit": round(est_profit, 2)
            })

    # Rank by total net profit
    deals.sort(key=lambda x: x["est_profit"], reverse=True)

    os.makedirs("ledgers", exist_ok=True)
    for p in ["ledgers/live_deals.json", "ledgers/sniper_deals.json"]:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(deals, f, indent=2)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(f"[+] Scan completed in {elapsed:.1f}s. Found {len(deals)} actionable deals. Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
