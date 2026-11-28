#!/usr/bin/env python3
"""
Hardened Live Deal Sniper Runner
================================
Queries liquid Forge items via `services.sde`, fetches Jita 4-4 order books,
and classifies deals via `services.market.evaluate_deal_opportunity()`.
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.esi import ESI_BASE_URL, REGION_THE_FORGE, JITA_STATION_ID, create_esi_session
from services.analytics_db import open_analytics_connection
from services.exclusions import is_item_excluded, load_active_blacklist, matches_sniper_banned_name_substring
from services.market import evaluate_deal_opportunity

def get_candidate_universe(scan_limit: int = 1200) -> dict[int, dict]:
    candidates: dict[int, dict] = {}
    rows = []
    try:
        with open_analytics_connection(read_only=True, attach_sde=False) as con:
            rows = con.execute(f"""
                WITH stats AS (
                    SELECT
                        type_id,
                        ROUND(AVG(volume), 1) AS daily_vol,
                        COUNT(DISTINCT TRY_CAST(date AS DATE)) AS active_days,
                        QUANTILE_CONT(average, 0.50) AS median_30d
                    FROM forge_market_history
                    WHERE TRY_CAST(date AS DATE) >= (SELECT MAX(TRY_CAST(date AS DATE)) - INTERVAL 30 DAY FROM forge_market_history)
                      AND average > 0 AND volume > 0
                    GROUP BY type_id
                    HAVING COUNT(DISTINCT TRY_CAST(date AS DATE)) >= 12
                       AND AVG(volume) >= 3.0
                       AND QUANTILE_CONT(average, 0.50) >= 10000000
                       AND (AVG(volume) * QUANTILE_CONT(average, 0.50)) >= 40000000
                )
                SELECT s.type_id, t.typeName, s.daily_vol, s.median_30d
                FROM stats s
                JOIN sde.invTypes t ON s.type_id = t.typeID
                JOIN sde.invGroups g ON t.groupID = g.groupID
                WHERE g.categoryID NOT IN (26, 30, 32, 63)
                ORDER BY (s.daily_vol * s.median_30d) DESC
                LIMIT {int(scan_limit)};
            """).fetchall()
    except Exception as e:
        print(f"[!] DuckDB Query Warning: {e}")

    bl_items, bl_groups, bl_meta = load_active_blacklist()
    for tid, name, daily_vol, median_30d in rows:
        if matches_sniper_banned_name_substring(name) or is_item_excluded(name, "", bl_items, bl_groups, bl_meta):
            continue
        candidates[int(tid)] = {
            "type_id": int(tid),
            "name": str(name),
            "median_30d": float(median_30d),
            "daily_vol": float(daily_vol),
        }
    return candidates


def scan_orders_for_type(session, tid: int):
    url = f"{ESI_BASE_URL}/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={tid}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code == 200:
            sell_orders, buy_orders = [], []
            for o in r.json():
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
    parser = argparse.ArgumentParser(description="Live Deal Sniper")
    parser.add_argument("--limit", type=int, default=600, help="Number of liquid items to scan (default: 600)")
    parser.add_argument("--mode", type=str, default="all", help="Scan mode compatibility flag")
    args = parser.parse_args()

    start_time = datetime.now(timezone.utc)
    candidates = get_candidate_universe(scan_limit=args.limit)
    if not candidates:
        print("[!] No candidates found to scan.")
        return

    session = create_esi_session()
    type_ids = list(candidates.keys())
    order_data = {}

    print(f"[*] Scanning live books across {len(type_ids)} targets...")
    with ThreadPoolExecutor(max_workers=28) as executor:
        futures = {executor.submit(scan_orders_for_type, session, tid): tid for tid in type_ids}
        for fut in as_completed(futures):
            tid, sells, top_buy = fut.result()
            if sells:
                order_data[tid] = (sells, top_buy)

    deals = []
    for tid, (sells, top_buy) in order_data.items():
        deal = evaluate_deal_opportunity(candidates[tid], sells, top_buy)
        if deal is not None:
            deals.append(deal)

    deals.sort(key=lambda x: x["est_profit"], reverse=True)

    target = "ledgers/sniper_deals.json"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(deals, f, indent=2)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(f"[+] Scan completed in {elapsed:.1f}s. Found {len(deals)} legitimate deals.")


if __name__ == "__main__":
    main()
