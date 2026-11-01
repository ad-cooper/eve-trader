from services.blacklist import load_active_blacklist
#!/usr/bin/env python3
"""
EVE Online Jita 4-4 High-Speed Deal Sniper (Velocity-Gated)
===========================================================
- Strict volume gate: daily_vol_30d >= 1.5 (purges Officer & ultra-illiquid modules).
- Dynamic sizing: drops items where a single unit exceeds safe turnover capacity.
- Concurrently scans top 900 liquid books in ~10 seconds.
"""

import os
import sys
import json
import time
import math
import argparse
import duckdb
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
OUTPUT_FILE = "ledgers/sniper_deals.json"

JITA_STATION_ID = 60003760
REGION_THE_FORGE = 10000002

BUY_FEE_TAKER = 0.00    # Direct taker buy = 0% broker fee
EXIT_FEE_JITA = 0.04605 # 1.23% broker + 3.375% sales tax (Hippie Father)

CONCURRENT_WORKERS = 16
MAX_CANDIDATE_BOOKS = 900


def create_session():
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=CONCURRENT_WORKERS, pool_maxsize=CONCURRENT_WORKERS * 2, max_retries=retries)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "EVE-VelocityGated-Sniper/7.0"})
    return session

def load_sniper_candidates():
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
    
    query = """
    WITH metrics_180d AS (
        SELECT 
            type_id,
            COUNT(*) AS total_history_days,
            MAX(average) AS high_180d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_180d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL 180 DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_30d AS (
        SELECT 
            type_id,
            AVG(volume) AS daily_vol_30d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_30d,
            AVG((highest - lowest) / NULLIF(average, 0)) * 100.0 AS avg_spread_30d,
            (COUNT(CASE WHEN (average - lowest) / NULLIF(average, 0) >= 0.045 THEN 1 END) * 100.0 / COUNT(*)) AS dump_freq_pct
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_7d AS (
        SELECT 
            type_id,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_7d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL 7 DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    )
    SELECT 
        m.type_id,
        t.typeName AS item_name,
        g.groupName AS group_name,
        c.categoryName AS category_name,
        l.vwap_180d / 1e6 AS price_m_180d,
        m.vwap_30d / 1e6 AS price_m_30d,
        w.vwap_7d / 1e6 AS price_m_7d,
        m.daily_vol_30d,
        m.dump_freq_pct,
        m.avg_spread_30d,
        (m.daily_vol_30d * (m.vwap_30d / 1e6)) AS daily_turnover_m
    FROM metrics_30d m
    JOIN metrics_7d w ON m.type_id = w.type_id
    JOIN metrics_180d l ON m.type_id = l.type_id
    JOIN sde.invTypes t ON m.type_id = t.typeID
    JOIN sde.invGroups g ON t.groupID = g.groupID
    JOIN sde.invCategories c ON g.categoryID = c.categoryID
    WHERE t.published = 1;
    """
    df = con.execute(query).df()
    con.close()
    return df

def fetch_book(session: requests.Session, type_id: int):
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code == 200:
            orders = [o for o in r.json() if o.get("location_id") == JITA_STATION_ID]
            sells = sorted([o for o in orders if not o.get("is_buy_order")], key=lambda x: x["price"])
            return type_id, sells
    except Exception:
        pass
    return type_id, []

def evaluate_deal(item_row, sells):
    if len(sells) < 2:
        return None

    best_sell = sells[0]
    second_sell = sells[1]

    ask_price = best_sell["price"]
    benchmark_sell = second_sell["price"]
    vwap_30d = item_row["price_m_30d"] * 1e6
    vwap_180d = item_row["price_m_180d"] * 1e6
    vwap_7d = item_row["price_m_7d"] * 1e6

    # 1. Bubble Guard: Skip artificially inflated listings
    if ask_price > (vwap_180d * 1.80) or ask_price > (vwap_30d * 1.35):
        return None

    # 2. Velocity Guard: Position sizing must NOT exceed 25% of daily volume
    # Purges items where 1 single unit exceeds safe turnover velocity
    daily_vol = item_row["daily_vol_30d"]
    qty_avail = best_sell["volume_remain"]
    safe_cap_units = int(daily_vol * 0.25)
    
    if safe_cap_units < 1:
        return None
        
    safe_qty = min(qty_avail, safe_cap_units)
    if safe_qty < 1:
        return None

    # 3. Strict Exit Anchor: Capped by the 2nd lowest active sell order
    target_exit = min(benchmark_sell - 0.01, vwap_30d * 1.08)

    undercut_vs_2nd = ((benchmark_sell - ask_price) / benchmark_sell) * 100.0
    vwap_discount = ((vwap_30d - ask_price) / vwap_30d) * 100.0

    deal_type = None
    if undercut_vs_2nd >= 7.5:
        deal_type = "FIRESALE DUMP"
        discount_pct = undercut_vs_2nd
    elif vwap_discount >= 10.0 and undercut_vs_2nd >= 3.5 and vwap_7d >= (vwap_30d * 0.85):
        deal_type = "VALUE DUMP"
        discount_pct = vwap_discount

    if not deal_type or target_exit <= ask_price:
        return None

    proceeds_unit = target_exit * (1.0 - EXIT_FEE_JITA)
    profit_unit = proceeds_unit - ask_price
    net_margin = (profit_unit / ask_price) * 100.0

    # 3.5% minimum net margin floor after Jita fees
    if net_margin < 3.5:
        return None

    cost_line = ask_price * safe_qty
    total_profit = profit_unit * safe_qty

    if total_profit < 10.0e6:
        return None

    return {
        "type_id": int(item_row["type_id"]),
        "name": item_row["item_name"],
        "deal_type": deal_type,
        "qty": safe_qty,
        "qty_avail": qty_avail,
        "ask_price": ask_price,
        "target_exit": target_exit,
        "discount_pct": discount_pct,
        "net_margin": net_margin,
        "capital": cost_line,
        "profit": total_profit
    }

def main():
    t_start = time.time()
    print(f"[*] Auditing universe for velocity-gated Jita firesales...")
    df = load_sniper_candidates()

    event_filter = "Radiance|Cerebral|Booster|Keypass|Cipher|Harvest|Tetrimon|Shortcut|Gauntlet|Medallion|Voucher|Token|Ticket|Pamphlet"
    xl_weapon_filter = "Dual Giga|Quad Mega|XL |Extra Large"

    bl_items, bl_groups, _ = load_active_blacklist()
    mask = ~df["group_name"].isin(bl_groups)
    mask &= ~df["item_name"].isin(bl_items)
    mask &= ~df["category_name"].isin(["Personalization", "SKINs", "Special Edition Assets", "Apparel"])
    mask &= ~df["item_name"].str.contains(event_filter, case=False, na=False)
    mask &= ~df["item_name"].str.contains(xl_weapon_filter, case=False, na=False)
    
    # Strict Velocity Gates
    mask &= df["daily_vol_30d"] >= 1.5      # At least 1.5 units/day (purges Officer & ultra-rare loot)
    mask &= df["daily_turnover_m"] >= 40.0  # At least 40M ISK/day traded
    mask &= df["price_m_30d"] >= 10.0       # At least 10M ISK unit price
    mask &= df["avg_spread_30d"] >= 5.0     # Healthy spread
    mask &= df["price_m_7d"] >= (df["price_m_30d"] * 0.82)

    candidates = df[mask].copy()
    candidates["alpha_score"] = candidates["avg_spread_30d"] * (candidates["dump_freq_pct"] / 100.0) * candidates["daily_turnover_m"].apply(lambda x: math.log10(max(10, x)))
    candidates = candidates.sort_values(by="alpha_score", ascending=False).head(MAX_CANDIDATE_BOOKS)
    
    cand_dict = {int(r["type_id"]): r for _, r in candidates.iterrows()}
    print(f"[+] Inspecting {len(candidates)} high-velocity books using {CONCURRENT_WORKERS} parallel workers...")

    deals = []
    session = create_session()

    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
        futures = {executor.submit(fetch_book, session, tid): tid for tid in cand_dict.keys()}
        for fut in as_completed(futures):
            tid, sells = fut.result()
            if not sells or tid not in cand_dict:
                continue
            deal = evaluate_deal(cand_dict[tid], sells)
            if deal:
                deals.append(deal)

    deals = sorted(deals, key=lambda x: x["profit"], reverse=True)
    t_elapsed = time.time() - t_start

    print("\n" + "=" * 135)
    print(f"--- JITA 4-4 REAL-TIME SNIPER RESULTS ({len(deals)} DEALS FOUND IN {t_elapsed:.1f}s) ---")
    print("=" * 135)
    print(f"{'#':<3} | {'ITEM NAME':<33} | {'TYPE':<16} | {'QTY':<5} | {'ASK PRICE':<12} | {'TARGET EXIT':<12} | {'DISC %':<7} | {'NET %':<6} | {'NET PROFIT'}")
    print("=" * 135)
    for idx, d in enumerate(deals, 1):
        b_str = f"{d['ask_price']/1e6:.2f}M" if d['ask_price'] >= 1e6 else f"{d['ask_price']:,.0f}"
        s_str = f"{d['target_exit']/1e6:.2f}M" if d['target_exit'] >= 1e6 else f"{d['target_exit']:,.0f}"
        p_str = f"{d['profit']/1e6:+.1f}M" if abs(d['profit']) >= 1e6 else f"{d['profit']:+,.0f}"
        print(f"{idx:<3} | {d['name'][:32]:<33} | {d['deal_type']:<16} | {d['qty']:>5} | {b_str:>12} | {s_str:>12} | {d['discount_pct']:>6.1f}% | {d['net_margin']:>5.1f}% | {p_str:>10}")
    print("=" * 135)

    os.makedirs("ledgers", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(deals, f, indent=2)

    print(f"[+] Saved structured deals to {OUTPUT_FILE} (Completed in {t_elapsed:.1f}s)\n")

if __name__ == "__main__":
    main()
