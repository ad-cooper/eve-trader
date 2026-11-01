#!/usr/bin/env python3
"""
Live ESI Order Book Target Validator for Jita 4-4
Queries live market depth, identifies top buy and bottom sell orders,
and calculates net profit after Upwell entry (0.5%) and Jita exit (4.605%).
"""

import time
import requests
import pandas as pd

JITA_STATION_ID = 60003760
REGION_THE_FORGE = 10000002

# Upwell entry: 0.5% SCC surcharge (0% citadel fee)
# Jita 4-4 exit: 1.23% broker fee + 3.375% sales tax = 4.605%
BUY_FEE_UPWELL = 0.005
EXIT_FEE_JITA = 0.04605
BREAKEVEN_GROSS = ((1.0 + BUY_FEE_UPWELL) / (1.0 - EXIT_FEE_JITA) - 1.0) * 100.0  # ~5.38%

# The 11 Evergreen candidates
EVERGREEN_ITEMS = [
    {"type_id": 56715, "name": "Cultural Semiosis Data", "qty": 45},
    {"type_id": 22110, "name": "Mid-grade Crystal Epsilon", "qty": 18},
    {"type_id": 33820, "name": "Barghest", "qty": 4},
    {"type_id": 33846, "name": "Dread Guristas Drone Damage Amp", "qty": 40},
    {"type_id": 33681, "name": "Gecko", "qty": 12},
    {"type_id": 18973, "name": "Core X-Type EM Armor Hardener", "qty": 10},
    {"type_id": 54753, "name": "Large Vorton Projector II", "qty": 4},
    {"type_id": 52997, "name": "Precursor Dreadnought Skillbook", "qty": 10},
    {"type_id": 1978,  "name": "Tracking Computer II", "qty": 400},
    {"type_id": 21740, "name": "Caldari Navy Antimatter Charge L", "qty": 100000},
    {"type_id": 31824, "name": "Medium Core Defense Op Solidifier II", "qty": 20},
]

def fetch_jita_orders(type_id: int):
    """Pulls current open orders for The Forge and isolates Jita 4-4."""
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}"
    headers = {"User-Agent": "EVE-Jita-Live-Target-Checker/1.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return [o for o in r.json() if o.get("location_id") == JITA_STATION_ID]
    except Exception as e:
        print(f"[-] ESI error on type {type_id}: {e}")
    return []

def main():
    print(f"[+] Querying live ESI order books for {len(EVERGREEN_ITEMS)} items in Jita 4-4...")
    print(f"[+] Upwell Entry Fee: {BUY_FEE_UPWELL*100:.2f}% | Jita Exit Drag: {EXIT_FEE_JITA*100:.3f}%")
    print(f"[+] Minimum Gross Spread to Break Even: {BREAKEVEN_GROSS:.2f}%\n")

    results = []

    for item in EVERGREEN_ITEMS:
        tid = item["type_id"]
        name = item["name"]
        qty = item["qty"]

        orders = fetch_jita_orders(tid)
        buy_orders = [o for o in orders if o.get("is_buy_order")]
        sell_orders = [o for o in orders if not o.get("is_buy_order")]

        if not buy_orders or not sell_orders:
            print(f"[-] {name}: Incomplete order book on ESI.")
            continue

        best_buy = max(o["price"] for o in buy_orders)
        best_sell = min(o["price"] for o in sell_orders)

        # Real Execution Targets
        target_buy = best_buy + 0.01
        target_sell = best_sell - 0.01

        # Real Margins
        cost_per_unit = target_buy * (1.0 + BUY_FEE_UPWELL)
        net_proceeds_per_unit = target_sell * (1.0 - EXIT_FEE_JITA)
        unit_profit = net_proceeds_per_unit - cost_per_unit
        gross_spread_pct = ((target_sell - target_buy) / target_buy) * 100.0
        net_margin_pct = (unit_profit / cost_per_unit) * 100.0

        total_capital = cost_per_unit * qty
        total_profit = unit_profit * qty

        results.append({
            "name": name,
            "qty": qty,
            "live_max_buy": best_buy,
            "target_buy": target_buy,
            "live_min_sell": best_sell,
            "target_sell": target_sell,
            "gross_spread": gross_spread_pct,
            "unit_profit": unit_profit,
            "net_margin": net_margin_pct,
            "total_capital": total_capital,
            "total_profit": total_profit
        })
        time.sleep(0.04)

    df = pd.DataFrame(results).sort_values(by="total_profit", ascending=False)

    print("=" * 135)
    print(f"{'ITEM NAME':<33} | {'QTY':<5} | {'LIVE TOP BUY':<12} | {'LIVE MIN SELL':<13} | {'GROSS %':<7} | {'NET %':<6} | {'CAPITAL':<9} | {'NET PROFIT'}")
    print("=" * 135)
    for _, r in df.iterrows():
        b_str = f"{r['target_buy']/1e6:.2f}M" if r['target_buy'] >= 1e6 else f"{r['target_buy']:,.0f}"
        s_str = f"{r['target_sell']/1e6:.2f}M" if r['target_sell'] >= 1e6 else f"{r['target_sell']:,.0f}"
        c_str = f"{r['total_capital']/1e9:.2f}B" if r['total_capital'] >= 1e9 else f"{r['total_capital']/1e6:.0f}M"
        p_str = f"{r['total_profit']/1e6:+.1f}M" if abs(r['total_profit']) >= 1e6 else f"{r['total_profit']:+,.0f}"
        print(f"{r['name'][:32]:<33} | {r['qty']:>5} | {b_str:>12} | {s_str:>13} | {r['gross_spread']:>6.1f}% | {r['net_margin']:>5.1f}% | {c_str:>9} | {p_str:>10}")
    print("=" * 135)

    tot_cap = df["total_capital"].sum()
    tot_prof = df["total_profit"].sum()
    print(f"\n[+] Total Live Capital Required: {tot_cap/1e9:.2f}B ISK")
    print(f"[+] Total Live Net Profit:       {tot_prof/1e9:.2f}B ISK")
    print(f"[+] Blended Live Net ROI:        {(tot_prof / tot_cap) * 100:.2f}%\n")

if __name__ == "__main__":
    main()
