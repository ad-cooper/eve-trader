#!/usr/bin/env python3
"""
Master 20B Passive Jita Portfolio Generator (Consolidated & Calibrated)
=====================================================================
1. Filters out confirmed one-sided sell traps (Dump Freq >= 55%, Dump Depth >= 4.5%).
2. Explicitly blacklists builder-only groups (Precursor Turrets, Components, PI).
3. Excludes seasonal event items and vanity assets.
4. Live ESI Spread Gate: >= 9.0% Gross Spread | Net Margin >= 4.0%.
5. Proportional passive sizing (<= 4.5% volume, 1 unit on items >= 250M).
6. High-impact profit floor (>= 15M ISK profit per line) to prevent order bloat.
7. Auto-ranks by net profit and allocates up to ~20.0B ISK capital.
"""

import os
import time
import requests
import pandas as pd

AUDIT_PATH = "data/jita_bid_liquidity_audit.csv"
DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"

JITA_STATION_ID = 60003760
REGION_THE_FORGE = 10000002

BUY_FEE_UPWELL = 0.005   # 0.5% SCC fee
EXIT_FEE_JITA = 0.04605  # 1.23% broker + 3.375% sales tax (Hippie Father)

MIN_LIVE_SPREAD = 9.0    # Minimum gross spread %
MAX_LIVE_SPREAD = 60.0   # Rejects empty-book troll bids
MIN_NET_MARGIN = 4.0     # Minimum net margin % after all fees
MIN_LINE_PROFIT = 15.0e6 # Filters micro-order spam (<15M profit)
TARGET_CAPITAL = 20.0e9  # 20B ISK target

# Groups that are structurally dominated by builders with zero loot dumps
BUILDER_GROUP_BLACKLIST = [
    "Precursor Turret",               # Ultratidal Entropic Disintegrators
    "Protective Components",          # Core Temperature Regulators
    "Capital Construction Components",
    "Construction Components",
    "Structure Components",
    "Planetary Commodities",
    "Fuel Block"
]

def load_verified_candidates():
    """Loads audit data and applies structural and empirical liquidity gates."""
    df = pd.read_csv(AUDIT_PATH)
    
    # 1. Empirical Dump Gates (Traps sit at <30% freq, <3% depth)
    mask = ~df["liquidity_verdict"].str.contains("RULE OUT", na=False)
    mask &= df["dump_freq_pct"] >= 55.0
    mask &= df["avg_dump_depth"] >= 4.5
    
    # 2. Structural Category Blacklist
    mask &= ~df["group_name"].isin(BUILDER_GROUP_BLACKLIST)
    
    # 3. Exclude seasonal event items and vanity assets
    event_filter = "Radiance|Cerebral|Booster|Keypass|Cipher|Harvest|Tetrimon|Shortcut|Gauntlet"
    mask &= ~df["item_name"].str.contains(event_filter, case=False, na=False)
    mask &= ~df["category_name"].isin(["Personalization", "SKINs", "Special Edition Assets", "Apparel"])
    
    # 4. Minimum market depth requirement (>400M ISK/day)
    df["turnover_m"] = (df["daily_vol"] * df["price_m"]).round(1)
    mask &= df["turnover_m"] >= 400.0
    
    return df[mask].sort_values(by="turnover_m", ascending=False)

def fetch_jita_live_book(type_id: int, retries=2):
    """Pulls current open orders in Jita 4-4 from ESI with retries."""
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}"
    headers = {"User-Agent": "EVE-20B-Master-Builder/2.0"}
    for _ in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=14)
            if r.status_code == 200:
                orders = [o for o in r.json() if o.get("location_id") == JITA_STATION_ID]
                buys = [o["price"] for o in orders if o.get("is_buy_order")]
                sells = [o["price"] for o in orders if not o.get("is_buy_order")]
                if buys and sells:
                    return max(buys), min(sells)
        except Exception:
            time.sleep(0.4)
    return None, None

def calculate_passive_qty(price_m: float, daily_vol: float) -> int:
    """Calculates order quantity capped strictly at <= 4.5% daily depth."""
    if price_m <= 0:
        return 1
    
    # High-ticket hulls, deadspace modules, capital books (>= 250M): exactly 1 unit
    if price_m >= 250.0:
        return 1
    # High-value items (100M - 250M): 2 units max
    elif price_m >= 100.0:
        return max(1, min(2, int(daily_vol * 0.035)))
    # High-turnover bulk commodities / filaments (daily_vol >= 100): up to 4% volume (max 25 units)
    elif daily_vol >= 100.0 and price_m >= 10.0:
        return max(2, min(25, int(daily_vol * 0.04)))
    # Mid-tier modules (30M - 100M): target ~250M-450M allocation, <= 4.5% vol
    elif price_m >= 30.0:
        units_target = max(2, int(350.0 / price_m))
        vol_limit = max(2, int(daily_vol * 0.045))
        return min(units_target, vol_limit)
    # Low-price modules/rigs (5M - 30M)
    elif price_m >= 5.0:
        units_target = max(5, int(250.0 / price_m))
        vol_limit = max(5, int(daily_vol * 0.045))
        return min(units_target, vol_limit)
    # Ammo / micro commodities
    else:
        vol_limit = int(daily_vol * 0.05)
        return int(round(min(50000, vol_limit), -2))

def main():
    print("[+] Step 1: Loading & filtering verified 2-sided evergreen items...")
    candidates = load_verified_candidates()
    print(f"[+] Found {len(candidates)} high-turnover, verified dump-market candidates.")
    
    print("\n[+] Step 2: Scanning live Jita order books across candidates...")
    live_results = []
    
    for _, row in candidates.iterrows():
        tid = int(row["type_id"])
        name = row["item_name"]
        daily_vol = float(row["daily_vol"])
        price_m = float(row["price_m"])
        
        top_buy, min_sell = fetch_jita_live_book(tid)
        if top_buy is None or min_sell is None:
            continue
        
        gross_spread = ((min_sell - top_buy) / top_buy) * 100.0
        if gross_spread < MIN_LIVE_SPREAD or gross_spread > MAX_LIVE_SPREAD:
            continue
        
        target_buy = top_buy + 0.01
        target_sell = min_sell - 0.01
        
        cost_unit = target_buy * (1.0 + BUY_FEE_UPWELL)
        proceeds_unit = target_sell * (1.0 - EXIT_FEE_JITA)
        profit_unit = proceeds_unit - cost_unit
        net_margin_pct = (profit_unit / cost_unit) * 100.0
        
        if net_margin_pct < MIN_NET_MARGIN:
            continue
            
        qty = calculate_passive_qty(price_m, daily_vol)
        item_capital = cost_unit * qty
        item_profit = profit_unit * qty
        
        if item_profit < MIN_LINE_PROFIT:
            continue
            
        live_results.append({
            "name": name,
            "qty": qty,
            "daily_vol": daily_vol,
            "dump_freq": row["dump_freq_pct"],
            "target_buy": target_buy,
            "target_sell": target_sell,
            "gross_spread": gross_spread,
            "net_margin": net_margin_pct,
            "capital": item_capital,
            "profit": item_profit
        })
        time.sleep(0.04)

    df_live = pd.DataFrame(live_results)
    if df_live.empty:
        print("[-] No items matched all criteria.")
        return

    # Step 3: Sort by highest projected net profit
    df_live = df_live.sort_values(by="profit", ascending=False)
    
    # Step 4: Allocate up to ~20B ISK
    portfolio = []
    cumulative_capital = 0.0
    for _, item in df_live.iterrows():
        if cumulative_capital >= TARGET_CAPITAL:
            break
        portfolio.append(item)
        cumulative_capital += item["capital"]
        
    final_df = pd.DataFrame(portfolio).sort_values(by="capital", ascending=False)
    
    print("\n" + "=" * 135)
    print(f"{'#':<3} | {'ITEM NAME':<33} | {'QTY':<5} | {'DUMP FREQ':<9} | {'TARGET BUY':<12} | {'TARGET SELL':<12} | {'NET %':<6} | {'CAPITAL':<8} | {'NET PROFIT'}")
    print("=" * 135)
    for idx, r in enumerate(final_df.to_dict('records'), 1):
        b_str = f"{r['target_buy']/1e6:.2f}M" if r['target_buy'] >= 1e6 else f"{r['target_buy']:,.0f}"
        s_str = f"{r['target_sell']/1e6:.2f}M" if r['target_sell'] >= 1e6 else f"{r['target_sell']:,.0f}"
        c_str = f"{r['capital']/1e9:.2f}B" if r['capital'] >= 1e9 else f"{r['capital']/1e6:.0f}M"
        p_str = f"{r['profit']/1e6:+.1f}M" if abs(r['profit']) >= 1e6 else f"{r['profit']:+,.0f}"
        print(f"{idx:<3} | {r['name'][:32]:<33} | {r['qty']:>5} | {r['dump_freq']:>8.1f}% | {b_str:>12} | {s_str:>12} | {r['net_margin']:>5.1f}% | {c_str:>8} | {p_str:>10}")
    print("=" * 135)
    
    tot_cap = final_df["capital"].sum()
    tot_prof = final_df["profit"].sum()
    print(f"\n[+] Total Active Line Items:     {len(final_df)}")
    print(f"[+] Total Live Capital Deployed: {tot_cap/1e9:.2f}B ISK")
    print(f"[+] Total Live Net Profit:       {tot_prof/1e9:.2f}B ISK")
    print(f"[+] Blended Live Net ROI:        {(tot_prof / tot_cap) * 100:.2f}%\n")

if __name__ == "__main__":
    main()
