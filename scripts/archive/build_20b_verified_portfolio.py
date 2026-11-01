#!/usr/bin/env python3
"""
Audit-Driven 20B Live Portfolio Builder (Balanced Allocation Engine)
- Filters out all One-Sided Sell Traps (requires Dump Freq >= 55%, Dump Depth >= 4.5%).
- Includes high-ticket ships/implants and bulk commodities with verified dump flow.
- Proportional capital sizing targeting ~20B ISK across 35-45 manageable lines.
"""

import time
import requests
import pandas as pd

AUDIT_PATH = "data/jita_bid_liquidity_audit.csv"
JITA_STATION_ID = 60003760
REGION_THE_FORGE = 10000002

BUY_FEE_UPWELL = 0.005   # 0.5% SCC fee
EXIT_FEE_JITA = 0.04605  # 1.23% broker + 3.375% sales tax
MIN_LIVE_SPREAD = 9.5    # Enforce minimum ~4.8% net margin floor
MAX_LIVE_SPREAD = 60.0   # Discard empty-book / placeholder bids
TARGET_CAPITAL = 20.0e9  # 20B ISK target

def load_verified_candidates():
    """Loads passed and viable evergreen items from the audit CSV."""
    df = pd.read_csv(AUDIT_PATH)
    
    # Strictly exclude confirmed one-sided traps and tight spread traps
    mask = ~df["liquidity_verdict"].str.contains("RULE OUT", na=False)
    
    # Require verified multi-day dump liquidity
    mask &= df["dump_freq_pct"] >= 55.0
    mask &= df["avg_dump_depth"] >= 4.5
    
    # Exclude expiring seasonal items and cosmetics
    event_filter = "Radiance|Cerebral|Booster|Keypass|Cipher|Harvest|Tetrimon|Shortcut|Gauntlet"
    mask &= ~df["item_name"].str.contains(event_filter, case=False, na=False)
    mask &= ~df["category_name"].isin(["Personalization", "SKINs", "Special Edition Assets", "Apparel"])
    
    # Rank by daily ISK turnover (daily_vol * price_m)
    df["turnover_m"] = (df["daily_vol"] * df["price_m"]).round(1)
    mask &= df["turnover_m"] >= 500.0  # Minimum 500M ISK/day depth
    
    return df[mask].sort_values(by="turnover_m", ascending=False)

def fetch_jita_orders(type_id: int, retries=2):
    """Pulls open orders with retry logic and longer timeout for heavy items."""
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}"
    headers = {"User-Agent": "EVE-20B-Verified-Portfolio/3.0"}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                return [o for o in r.json() if o.get("location_id") == JITA_STATION_ID]
        except Exception:
            time.sleep(0.5)
    return []

def calculate_balanced_qty(price_m: float, daily_vol: float) -> int:
    """
    Sizing rules to keep lines passive while deploying ~20B across ~40 lines:
    - High-ticket (>= 250M): exactly 1 unit (Barghest, Bhaalgorn, Vorton II, Leopard).
    - Mid-ticket (100M - 250M): 2 to 3 units (~300M - 500M allocation).
    - Bulk commodities / High-volume (daily_vol >= 100): up to 4% volume (max 25 units).
    - Mid-tier modules (10M - 100M): 3 to 6 units (~200M - 350M allocation, <= 4.5% vol).
    """
    if price_m <= 0:
        return 1
    
    if price_m >= 250.0:
        return 1
    elif price_m >= 100.0:
        return max(1, min(3, max(1, int(daily_vol * 0.035))))
    elif daily_vol >= 100.0 and price_m >= 10.0:
        # High volume commodities like Cultural Semiosis Data
        return max(2, min(25, int(daily_vol * 0.04)))
    elif price_m >= 10.0:
        # Target ~250M - 350M capital per module line
        target_units = max(2, int(300.0 / price_m))
        vol_limit = max(2, int(daily_vol * 0.045))
        return min(target_units, vol_limit)
    elif price_m >= 1.0:
        target_units = max(10, int(250.0 / price_m))
        vol_limit = max(10, int(daily_vol * 0.045))
        return min(target_units, vol_limit)
    else:
        # Faction ammo / micro commodities
        return int(round(min(50000, daily_vol * 0.05), -2))

def main():
    candidates = load_verified_candidates()
    print(f"[+] Loaded {len(candidates)} verified two-sided evergreen items from audit.")
    print(f"[+] Scanning live Jita order books to allocate ~20B ISK across ~35-45 items...\n")
    
    portfolio = []
    deployed_capital = 0.0

    for _, row in candidates.iterrows():
        if deployed_capital >= TARGET_CAPITAL:
            break

        tid = int(row["type_id"])
        name = row["item_name"]
        daily_vol = float(row["daily_vol"])
        price_m = float(row["price_m"])

        qty = calculate_balanced_qty(price_m, daily_vol)

        orders = fetch_jita_orders(tid)
        buy_orders = [o for o in orders if o.get("is_buy_order")]
        sell_orders = [o for o in orders if not o.get("is_buy_order")]

        if not buy_orders or not sell_orders:
            continue

        best_buy = max(o["price"] for o in buy_orders)
        best_sell = min(o["price"] for o in sell_orders)

        gross_spread = ((best_sell - best_buy) / best_buy) * 100.0

        if gross_spread < MIN_LIVE_SPREAD or gross_spread > MAX_LIVE_SPREAD:
            continue

        target_buy = best_buy + 0.01
        target_sell = best_sell - 0.01

        cost_unit = target_buy * (1.0 + BUY_FEE_UPWELL)
        proceeds_unit = target_sell * (1.0 - EXIT_FEE_JITA)
        profit_unit = proceeds_unit - cost_unit
        net_margin_pct = (profit_unit / cost_unit) * 100.0

        item_cap = cost_unit * qty
        item_prof = profit_unit * qty

        portfolio.append({
            "name": name,
            "qty": qty,
            "daily_vol": daily_vol,
            "dump_freq": row["dump_freq_pct"],
            "target_buy": target_buy,
            "target_sell": target_sell,
            "gross_spread": gross_spread,
            "net_margin": net_margin_pct,
            "capital": item_cap,
            "profit": item_prof
        })

        deployed_capital += item_cap
        time.sleep(0.04)

    df_res = pd.DataFrame(portfolio).sort_values(by="capital", ascending=False)

    print("=" * 135)
    print(f"{'ITEM NAME':<33} | {'QTY':<5} | {'DUMP FREQ':<9} | {'TARGET BUY':<12} | {'TARGET SELL':<12} | {'NET %':<6} | {'CAPITAL':<8} | {'NET PROFIT'}")
    print("=" * 135)
    for _, r in df_res.iterrows():
        b_str = f"{r['target_buy']/1e6:.2f}M" if r['target_buy'] >= 1e6 else f"{r['target_buy']:,.0f}"
        s_str = f"{r['target_sell']/1e6:.2f}M" if r['target_sell'] >= 1e6 else f"{r['target_sell']:,.0f}"
        c_str = f"{r['capital']/1e9:.2f}B" if r['capital'] >= 1e9 else f"{r['capital']/1e6:.0f}M"
        p_str = f"{r['profit']/1e6:+.1f}M" if abs(r['profit']) >= 1e6 else f"{r['profit']:+,.0f}"
        print(f"{r['name'][:32]:<33} | {r['qty']:>5} | {r['dump_freq']:>8.1f}% | {b_str:>12} | {s_str:>12} | {r['net_margin']:>5.1f}% | {c_str:>8} | {p_str:>10}")
    print("=" * 135)

    tot_cap = df_res["capital"].sum()
    tot_prof = df_res["profit"].sum()
    print(f"\n[+] Total Line Items:            {len(df_res)}")
    print(f"[+] Total Live Capital Deployed: {tot_cap/1e9:.2f}B ISK")
    print(f"[+] Total Live Net Profit:       {tot_prof/1e9:.2f}B ISK")
    print(f"[+] Blended Live Net ROI:        {(tot_prof / tot_cap) * 100:.2f}%\n")

if __name__ == "__main__":
    main()
