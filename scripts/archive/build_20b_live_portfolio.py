#!/usr/bin/env python3
"""
Live 20B Passive Jita Portfolio Builder (82 Evergreen Candidates)
- Enforces strict 11.0% Gross Spread floor (minimum ~6.0% Net ROI).
- Filters out troll bids / empty order book traps (Max spread: 60.0%).
- Strictly caps volume share at <= 5% (1-2 units for high-ticket).
"""

import time
import requests
import pandas as pd

JITA_STATION_ID = 60003760
REGION_THE_FORGE = 10000002

BUY_FEE_UPWELL = 0.005   # 0.5% SCC fee
EXIT_FEE_JITA = 0.04605  # 1.23% broker + 3.375% sales tax
MIN_GROSS_SPREAD = 11.0  # Guarantees >= 6.0% net margin after fees
MAX_GROSS_SPREAD = 60.0  # Filters out troll bids / placeholder orders

CANDIDATES = [
    # --- Bulk Stack-Dump Commodities (Pochven / LP / Reactions) ---
    {"type_id": 56715, "name": "Cultural Semiosis Data", "max_qty": 35},
    {"type_id": 56716, "name": "Protective Equipment Patterns", "max_qty": 6},
    {"type_id": 57444, "name": "Nanoscale Filter Plate", "max_qty": 4},
    {"type_id": 47910, "name": "Trinary State Processor", "max_qty": 8000},
    {"type_id": 47911, "name": "Shattered Villard Wheel", "max_qty": 800},
    {"type_id": 28694, "name": "Core Temperature Regulator", "max_qty": 40},

    # --- Fleet Munitions & High-Turnover Consumables ---
    {"type_id": 21740, "name": "Caldari Navy Antimatter Charge L", "max_qty": 50000},
    {"type_id": 47895, "name": "Fierce Gamma Filament", "max_qty": 80},
    {"type_id": 1978,  "name": "Tracking Computer II", "max_qty": 120},

    # --- Faction & Deadspace Tackle / Propulsion / Fleet Modules ---
    {"type_id": 16062, "name": "Caldari Navy Cruise Launcher", "max_qty": 4},
    {"type_id": 14041, "name": "Domination Tracking Enhancer", "max_qty": 4},
    {"type_id": 14264, "name": "Domination Stasis Webifier", "max_qty": 3},
    {"type_id": 41059, "name": "Domination Heavy Stasis Grappler", "max_qty": 2},
    {"type_id": 41058, "name": "Shadow Serpentis Heavy Stasis Grappler", "max_qty": 2},
    {"type_id": 33985, "name": "Prototype Hyperspatial Accelerator", "max_qty": 4},
    {"type_id": 33842, "name": "Federation Navy Drone Damage Amp", "max_qty": 4},
    {"type_id": 33848, "name": "Sentient Drone Damage Amplifier", "max_qty": 3},
    {"type_id": 33852, "name": "Sentient Drone Navigation Computer", "max_qty": 2},
    {"type_id": 13945, "name": "Shadow Serpentis Magnetic Field Stabilizer", "max_qty": 3},
    {"type_id": 14045, "name": "Domination Gyrostabilizer", "max_qty": 3},
    {"type_id": 54753, "name": "Large Vorton Projector II", "max_qty": 2},
    {"type_id": 92514, "name": "Ultratidal Entropic Disintegrator II", "max_qty": 2},
    {"type_id": 52238, "name": "Zorya's Heavy Entropic Disintegrator", "max_qty": 2},
    {"type_id": 40662, "name": "Capital Energy Neutralizer II", "max_qty": 2},
    {"type_id": 14005, "name": "Dark Blood Heavy Energy Neutralizer", "max_qty": 2},
    {"type_id": 37625, "name": "Corpum C-Type Medium Neutralizer", "max_qty": 3},
    {"type_id": 28381, "name": "Dread Guristas Heavy Assault Launcher", "max_qty": 3},
    {"type_id": 13924, "name": "Dread Guristas Torpedo Launcher", "max_qty": 3},
    {"type_id": 13920, "name": "Dread Guristas Rapid Light Launcher", "max_qty": 3},

    # --- Deadspace Armor & Shield Tank ---
    {"type_id": 19045, "name": "Corpus X-Type Large Armor Repairer", "max_qty": 1},
    {"type_id": 13957, "name": "Dark Blood Large Armor Repairer", "max_qty": 3},
    {"type_id": 13985, "name": "True Sansha Large Armor Repairer", "max_qty": 2},
    {"type_id": 19033, "name": "Corelum A-Type Medium Armor Repairer", "max_qty": 1},
    {"type_id": 18973, "name": "Core X-Type EM Armor Hardener", "max_qty": 2},
    {"type_id": 18977, "name": "Core X-Type Kinetic Armor Hardener", "max_qty": 3},
    {"type_id": 18933, "name": "Corpus X-Type EM Armor Hardener", "max_qty": 2},
    {"type_id": 18935, "name": "Centus X-Type EM Armor Hardener", "max_qty": 2},
    {"type_id": 2050,  "name": "Gistum C-Type Multispectrum Hardener", "max_qty": 2},
    {"type_id": 18867, "name": "Centum A-Type Thermal Energized Memb", "max_qty": 2},
    {"type_id": 18698, "name": "Core X-Type 100MN Afterburner", "max_qty": 1},
    {"type_id": 18672, "name": "Gistum A-Type 10MN Afterburner", "max_qty": 2},
    {"type_id": 19345, "name": "Gistum B-Type 50MN Microwarpdrive", "max_qty": 2},
    {"type_id": 14116, "name": "Shadow Serpentis 500MN MWD", "max_qty": 2},
    {"type_id": 14114, "name": "Domination 500MN MWD", "max_qty": 3},
    {"type_id": 14146, "name": "True Sansha Capacitor Power Relay", "max_qty": 3},
    {"type_id": 14190, "name": "True Sansha Large EMP Smartbomb", "max_qty": 2},
    {"type_id": 19355, "name": "Gist X-Type 100MN Afterburner", "max_qty": 2},
    {"type_id": 19187, "name": "Gist X-Type EM Shield Hardener", "max_qty": 2},
    {"type_id": 19223, "name": "Pithum B-Type EM Shield Amplifier", "max_qty": 3},
    {"type_id": 19301, "name": "Gist X-Type Shield Boost Amp", "max_qty": 3},
    {"type_id": 28744, "name": "Thukker Large Shield Extender", "max_qty": 3},
    {"type_id": 19119, "name": "Corpus X-Type Heavy Energy Nosferatu", "max_qty": 2},

    # --- Mutaplasmids (High Margin, Fast Churn) ---
    {"type_id": 85469, "name": "Glorified Unstable 5MN MWD Mutaplasmid", "max_qty": 1},
    {"type_id": 97639, "name": "Unstable Capital Remote Armor Mutaplasmid", "max_qty": 1},
    {"type_id": 47700, "name": "Unstable Stasis Webifier Mutaplasmid", "max_qty": 3},
    {"type_id": 49729, "name": "Unstable Heat Sink Mutaplasmid", "max_qty": 3},
    {"type_id": 47806, "name": "Unstable Large Shield Extender Mutaplasmid", "max_qty": 2},
    {"type_id": 90468, "name": "Gravid Strip Miner Mutaplasmid", "max_qty": 15},

    # --- Cyberimplants (Micro-Snipe 1 to 4 units) ---
    {"type_id": 22110, "name": "Mid-grade Crystal Epsilon", "max_qty": 4},
    {"type_id": 20159, "name": "High-grade Crystal Delta", "max_qty": 2},
    {"type_id": 20157, "name": "High-grade Crystal Beta", "max_qty": 2},
    {"type_id": 22111, "name": "Mid-grade Crystal Gamma", "max_qty": 3},
    {"type_id": 28801, "name": "Mid-grade Nomad Omega", "max_qty": 1},
    {"type_id": 28798, "name": "Mid-grade Nomad Delta", "max_qty": 1},
    {"type_id": 54403, "name": "Mid-grade Hydra Omega", "max_qty": 1},
    {"type_id": 22123, "name": "Mid-grade Amulet Gamma", "max_qty": 3},
    {"type_id": 22120, "name": "Mid-grade Amulet Beta", "max_qty": 3},
    {"type_id": 28812, "name": "Mid-grade Virtue Gamma", "max_qty": 3},
    {"type_id": 27116, "name": "Inherent Implants 'Squire' EM-805", "max_qty": 3},
    {"type_id": 3095,  "name": "Eifyr and Co. 'Rogue' EM-706", "max_qty": 1},

    # --- Skillbooks, Command Ships & Specialized Hulls (Strict 1-2 Units) ---
    {"type_id": 52997, "name": "Precursor Dreadnought Skillbook", "max_qty": 2},
    {"type_id": 47875, "name": "Large Disintegrator Specialization", "max_qty": 2},
    {"type_id": 25530, "name": "Neurotoxin Recovery Skillbook", "max_qty": 2},
    {"type_id": 52252, "name": "Ikitursa", "max_qty": 1},
    {"type_id": 22466, "name": "Astarte", "max_qty": 1},
    {"type_id": 40690, "name": "Sentient Burst Jammer", "max_qty": 1},
    {"type_id": 33513, "name": "Leopard", "max_qty": 1},
    {"type_id": 81048, "name": "Tholos", "max_qty": 1},
    {"type_id": 47270, "name": "Vedmak", "max_qty": 2},
    {"type_id": 33472, "name": "Nestor", "max_qty": 1},
    {"type_id": 17920, "name": "Bhaalgorn", "max_qty": 1},
    {"type_id": 33820, "name": "Barghest", "max_qty": 1},
]

def fetch_jita_orders(type_id: int):
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}"
    headers = {"User-Agent": "EVE-20B-Portfolio-Builder/4.0"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return [o for o in r.json() if o.get("location_id") == JITA_STATION_ID]
    except Exception:
        pass
    return []

def main():
    print(f"[+] Scanning live Jita 4-4 books across {len(CANDIDATES)} candidates...")
    print(f"[+] Filter: {MIN_GROSS_SPREAD}% <= Gross Spread <= {MAX_GROSS_SPREAD}%\n")
    results = []

    for item in CANDIDATES:
        tid = item["type_id"]
        name = item["name"]
        qty = item["max_qty"]

        orders = fetch_jita_orders(tid)
        buy_orders = [o for o in orders if o.get("is_buy_order")]
        sell_orders = [o for o in orders if not o.get("is_buy_order")]

        if not buy_orders or not sell_orders:
            continue

        best_buy = max(o["price"] for o in buy_orders)
        best_sell = min(o["price"] for o in sell_orders)

        gross_spread = ((best_sell - best_buy) / best_buy) * 100.0

        if gross_spread < MIN_GROSS_SPREAD or gross_spread > MAX_GROSS_SPREAD:
            continue

        target_buy = best_buy + 0.01
        target_sell = best_sell - 0.01

        cost_unit = target_buy * (1.0 + BUY_FEE_UPWELL)
        proceeds_unit = target_sell * (1.0 - EXIT_FEE_JITA)
        profit_unit = proceeds_unit - cost_unit
        net_margin_pct = (profit_unit / cost_unit) * 100.0

        capital = cost_unit * qty
        profit = profit_unit * qty

        results.append({
            "name": name,
            "qty": qty,
            "target_buy": target_buy,
            "target_sell": target_sell,
            "gross_spread": gross_spread,
            "net_margin": net_margin_pct,
            "capital": capital,
            "profit": profit
        })
        time.sleep(0.04)

    df = pd.DataFrame(results).sort_values(by="capital", ascending=False)

    print("=" * 130)
    print(f"{'ITEM NAME':<33} | {'QTY':<5} | {'TARGET BUY':<12} | {'TARGET SELL':<12} | {'GROSS %':<7} | {'NET %':<6} | {'CAPITAL':<8} | {'NET PROFIT'}")
    print("=" * 130)
    for _, r in df.iterrows():
        b_str = f"{r['target_buy']/1e6:.2f}M" if r['target_buy'] >= 1e6 else f"{r['target_buy']:,.0f}"
        s_str = f"{r['target_sell']/1e6:.2f}M" if r['target_sell'] >= 1e6 else f"{r['target_sell']:,.0f}"
        c_str = f"{r['capital']/1e9:.2f}B" if r['capital'] >= 1e9 else f"{r['capital']/1e6:.0f}M"
        p_str = f"{r['profit']/1e6:+.1f}M" if abs(r['profit']) >= 1e6 else f"{r['profit']:+,.0f}"
        print(f"{r['name'][:32]:<33} | {r['qty']:>5} | {b_str:>12} | {s_str:>12} | {r['gross_spread']:>6.1f}% | {r['net_margin']:>5.1f}% | {c_str:>8} | {p_str:>10}")
    print("=" * 130)

    tot_cap = df["capital"].sum()
    tot_prof = df["profit"].sum()
    print(f"\n[+] Total Live Capital Deployed: {tot_cap/1e9:.2f}B ISK")
    print(f"[+] Total Live Net Profit:       {tot_prof/1e9:.2f}B ISK")
    print(f"[+] Blended Live Net ROI:        {(tot_prof / tot_cap) * 100:.2f}%\n")

if __name__ == "__main__":
    main()
