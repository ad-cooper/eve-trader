#!/usr/bin/env python3
"""
C-J6MT (1st Taj Mahgoon) vs Jita 4-4 ESI Market Scanner
Target Character: Skie ankora
"""

import os
import sys
import json
import base64
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import csv
from typing import Dict, List, Any
from staging_diff_engine import process_snapshot_diff

# ==============================================================================
# 1. APPLICATION CREDENTIALS & TARGETS
# ==============================================================================
CLIENT_ID = "c2dfe86e7fce4f39b9e87a1076e8d61b"
CLIENT_SECRET = "eat_1mH4QAqBnvDWx6ZOe13XGvgmraU60sMWz_45wGgJ"
CALLBACK_PORT = 8080
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"
TOKEN_FILE = "token_skie_ankora.json"

# Staging Structure IDs
C_J6MT_STRUCTURE_ID = 1049588174021   # 1st Taj Mahgoon (Keepstar)
UALX_3_STRUCTURE_ID = 1046664001931   # Mothership Bellicose (Keepstar)

# Market & Freight Constants
FREIGHT_RATE_PER_M3 = 800.0   # ISK / m3 from Jita to Nullsec
JITA_STATION_ID = 60003760    # Jita IV - Moon 4 - CN Assembly Plant
LOCAL_STAGING_FEE = 0.010     # 1.0% Keepstar Broker Fee

SCOPES = [
    "publicData",
    "esi-location.read_location.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-wallet.read_corporation_wallet.v1",
    "esi-search.search_structures.v1",
    "esi-universe.read_structures.v1",
    "esi-markets.structure_markets.v1",
    "esi-markets.read_character_orders.v1",
    "esi-wallet.read_corporation_wallets.v1",
    "esi-markets.read_corporation_orders.v1",
    "esi-structures.read_corporation.v1",
    "esi-structures.read_character.v1"
]

WATCHLIST_TYPES = {
    # Exhumers & Mining Barges
    "Hulk": 22544,
    "Mackinaw": 22548,
    "Skiff": 22546,
    "Covetor": 17480,
    "Retriever": 17478,
    "Procurer": 17476,
    "Porpoise": 42244,
    "Orca": 28606,

    # Mining Modules & Upgrades
    "ORE Mining Laser Upgrade": 28576,
    "Caldari Navy Mining Laser Upgrade": 54687,
    "Mining Laser Upgrade II": 22291,
    "ORE Strip Miner": 28578,
    "Modulated Strip Miner II": 17912,
    "Ice Harvester II": 22229,
    "Mining Foreman Mindlink": 22247,
    "Highwall Mining MX-1005": 27147,
    "Highwall Mining MX-1003": 27146,
    "Yeti Ice Harvesting BX-1005": 27150,

    # High-Margin Faction Combat Modules
    "Dread Guristas Rapid Heavy Missile Launcher": 29011,
    "Caldari Navy Ballistic Control System": 15681,
    "Republic Fleet Gyrostabilizer": 15806,
    "Federation Navy Magnetic Field Stabilizer": 15810,
    "Imperial Navy Heat Sink": 15891,
    "Sentient Damage Control": 41215,
    "Dread Guristas X-Large Shield Booster": 14234,
    "Pithum C-Type Multispectrum Shield Hardener": 19207,
    "Pithum A-Type Multispectrum Shield Hardener": 19211,
    "Gist X-Type 100MN Afterburner": 14065,
    "Federation Navy 100MN Afterburner": 14053,
    "Dark Blood Multispectrum Energized Membrane": 15289,
    "Dark Blood Heavy Energy Neutralizer": 15159,
    "True Sansha Medium Armor Repairer": 14144,
    "Dark Blood Large Capacitor Battery": 15335,

    # Combat Hulls (Marauders, Blops, Capitals)
    "Golem": 28656,
    "Vargur": 28665,
    "Paladin": 28659,
    "Kronos": 28661,
    "Redeemer": 22428,
    "Widow": 22436,
    "Sin": 22430,
    "Panther": 22440,
    "Revelation": 19720,
    "Naglfar": 19726,
    "Phoenix": 19724,
    "Apostle": 37604,
    "Minokawa": 37606,
}

# ==============================================================================
# 2. TOKEN MANAGEMENT
# ==============================================================================
def decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = token.split('.')
    if len(parts) >= 2:
        payload = parts[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode('utf-8'))
    return {}

def get_tokens() -> Dict[str, Any]:
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            token_data = json.load(f)
            refresh_payload = {
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"]
            }
            resp = requests.post(
                "https://login.eveonline.com/v2/oauth/token",
                data=refresh_payload,
                headers={
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Host": "login.eveonline.com"
                }
            )
            if resp.status_code == 200:
                new_tokens = resp.json()
                token_data["access_token"] = new_tokens["access_token"]
                if "refresh_token" in new_tokens:
                    token_data["refresh_token"] = new_tokens["refresh_token"]
                with open(TOKEN_FILE, "w") as fw:
                    json.dump(token_data, fw)
                return token_data

    print("[-] Token file not found. Re-run initial authentication.")
    sys.exit(1)

# ==============================================================================
# 3. ESI DATA FETCHING
# ==============================================================================
def resolve_structure_name(access_token: str, structure_id: int) -> str:
    url = f"https://esi.evetech.net/latest/universe/structures/{structure_id}/"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code == 200:
        return resp.json().get("name", "Unknown Structure")
    return "Private Upwell Structure"

def fetch_all_structure_orders(access_token: str, structure_id: int) -> List[Dict[str, Any]]:
    print(f"[+] Pulling all active market orders from Structure {structure_id}...")
    orders = []
    page = 1
    while True:
        url = f"https://esi.evetech.net/latest/markets/structures/{structure_id}/?page={page}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code != 200:
            break
        page_orders = resp.json()
        if not page_orders:
            break
        orders.extend(page_orders)
        max_pages = int(resp.headers.get("X-Pages", 1))
        if page >= max_pages:
            break
        page += 1
    print(f"[+] Loaded {len(orders)} active structure orders.")
    return orders

def get_universe_type_info(type_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Batch-resolves item names using smaller 500-item chunks to prevent ESI drops."""
    type_info = {}
    chunk_size = 500
    for i in range(0, len(type_ids), chunk_size):
        chunk = type_ids[i:i + chunk_size]
        url = "https://esi.evetech.net/latest/universe/names/"
        resp = requests.post(url, json=chunk)
        if resp.status_code == 200:
            for item in resp.json():
                type_info[item["id"]] = {"name": item["name"], "volume": 1.0}

    # Known packaged volumes for ships & bulky structures
    known_bulky = {
        # Mining Barges & Exhumers
        22544: 50000.0, 22548: 50000.0, 22546: 50000.0,  # Hulk, Mackinaw, Skiff
        17480: 3750.0, 17478: 3750.0, 17476: 3750.0,    # Covetor, Retriever, Procurer
        42244: 50000.0, 28606: 500000.0,                 # Porpoise, Orca
        # Marauders & Battleships
        28656: 50000.0, 28665: 50000.0, 28659: 50000.0, 28661: 50000.0, # Golem, Vargur, Paladin, Kronos
        22428: 50000.0, 22436: 50000.0, 22430: 50000.0, 22440: 50000.0, # Redeemer, Widow, Sin, Panther
        17736: 50000.0, 17738: 50000.0, 17740: 50000.0,                 # Megathron Navy, etc.
        # Cruisers & Battlecruisers
        3516: 10000.0,                                                   # Monitor
        # Capital Modules & Fighters
        # Defaults to 1000 m3 for capital mods if detected by name
    }

    for tid, data in type_info.items():
        if tid in known_bulky:
            data["volume"] = known_bulky[tid]
        elif "Capital" in data["name"]:
            data["volume"] = 1000.0
        elif "Blueprint" in data["name"] or "SKIN" in data["name"]:
            data["volume"] = 0.01

    return type_info

def get_jita_prices(type_ids: List[int]) -> Dict[int, Dict[str, float]]:
    prices = {}
    chunk_size = 100
    for i in range(0, len(type_ids), chunk_size):
        chunk = type_ids[i:i + chunk_size]
        types_str = ",".join(str(x) for x in chunk)
        url = f"https://market.fuzzwork.co.uk/aggregates/?station={JITA_STATION_ID}&types={types_str}"
        resp = requests.get(url)
        if resp.status_code == 200:
            data = resp.json()
            for tid_str, d in data.items():
                tid = int(tid_str)
                sell_min = float(d["sell"]["min"]) if d["sell"]["min"] else 0.0
                buy_max = float(d["buy"]["max"]) if d["buy"]["max"] else 0.0
                prices[tid] = {"jita_sell": sell_min, "jita_buy": buy_max}
    return prices

# ==============================================================================
# 4. ENGINE
# ==============================================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scan Nullsec Upwell Market against Jita 4-4")
    parser.add_argument("--hub", choices=["cj6", "ualx"], default="cj6",
                        help="Target staging hub (default: cj6)")
    args = parser.parse_args()

    struct_id = C_J6MT_STRUCTURE_ID if args.hub == "cj6" else UALX_3_STRUCTURE_ID

    tokens = get_tokens()
    access_token = tokens["access_token"]
    jwt_data = decode_jwt_payload(access_token)
    
    char_name = jwt_data.get("name", "Unknown Pilot")
    print(f"[+] Authorized as: {char_name}")

    struct_name = resolve_structure_name(access_token, struct_id)
    print(f"[+] Target Structure: {struct_name} (ID: {struct_id})")

    structure_orders = fetch_all_structure_orders(access_token, struct_id)
    # Run the snapshot diffing engine in DuckDB
    process_snapshot_diff(struct_id, structure_orders)

    local_sells = {}
    local_buys = {}
    all_seen_types = set(WATCHLIST_TYPES.values())

    for o in structure_orders:
        tid = o["type_id"]
        price = o["price"]
        all_seen_types.add(tid)

        if o["is_buy_order"]:
            if tid not in local_buys or price > local_buys[tid]:
                local_buys[tid] = price
        else:
            if tid not in local_sells or price < local_sells[tid]:
                local_sells[tid] = price

    type_list = list(all_seen_types)
    print(f"[+] Items to evaluate (Watchlist + Active Market): {len(type_list)}")

    print("[+] Resolving item names...")
    type_info = get_universe_type_info(type_list)

    print("[+] Pulling Jita 4-4 market benchmark data...")
    jita_prices = get_jita_prices(type_list)

    results = []
    for tid in type_list:
        name = type_info.get(tid, {}).get("name", f"Type {tid}")
        vol = type_info.get(tid, {}).get("volume", 1.0)

        j_sell = jita_prices.get(tid, {}).get("jita_sell", 0.0)
        c_sell = local_sells.get(tid, 0.0)

        freight_cost = vol * FREIGHT_RATE_PER_M3
        landed_cost = j_sell + freight_cost

        status = "ACTIVE"
        net_profit = 0.0
        margin_pct = 0.0

        if c_sell == 0.0 and j_sell > 0:
            status = "STOCKOUT (OPPORTUNITY)"
            suggested_sell = j_sell * 1.25  # 25% staging markup
            net_profit = suggested_sell * (1.0 - LOCAL_STAGING_FEE) - landed_cost
            margin_pct = (net_profit / landed_cost) * 100.0 if landed_cost > 0 else 0
        elif c_sell > 0 and j_sell > 0:
            net_profit = c_sell * (1.0 - LOCAL_STAGING_FEE) - landed_cost
            margin_pct = (net_profit / landed_cost) * 100.0 if landed_cost > 0 else 0
            if margin_pct < 12.0:
                status = "MARGIN SQUEEZED"
            elif margin_pct >= 25.0:
                status = "HIGH MARGIN"
        else:
            continue

        # Skip SKINs and items with negligible ISK profit (< 5M)
        if "SKIN" in name or net_profit < 5000000:
            continue

        results.append({
            "name": name,
            "type_id": tid,
            "volume_m3": vol,
            "jita_sell": j_sell,
            "landed_cost": landed_cost,
            "local_sell": c_sell,
            "net_profit": net_profit,
            "margin_pct": margin_pct,
            "status": status
        })

    # Sort by Net ISK Profit descending
    results.sort(key=lambda x: x["net_profit"], reverse=True)

    print("\n" + "="*112)
    print(f"{'ITEM NAME':<34} | {'VOL (m3)':<8} | {'JITA SELL':<11} | {'LANDED':<11} | {'STAGING SELL':<11} | {'MARGIN %':<8} | {'STATUS'}")
    print("="*112)

    watchlist_names = set(WATCHLIST_TYPES.keys())
    for r in results:
        if r["name"] in watchlist_names or r["margin_pct"] >= 25.0:
            j_str = f"{r['jita_sell']/1e6:.1f}M" if r['jita_sell'] < 1e9 else f"{r['jita_sell']/1e9:.2f}B"
            l_str = f"{r['landed_cost']/1e6:.1f}M" if r['landed_cost'] < 1e9 else f"{r['landed_cost']/1e9:.2f}B"
            c_str = f"{r['local_sell']/1e6:.1f}M" if (0 < r['local_sell'] < 1e9) else (f"{r['local_sell']/1e9:.2f}B" if r['local_sell'] >= 1e9 else "STOCKOUT")
            
            print(f"{r['name'][:33]:<34} | {r['volume_m3']:>8.1f} | {j_str:>11} | {l_str:>11} | {c_str:>11} | {r['margin_pct']:>7.1f}% | {r['status']}")

    print("="*112)

    # Export to CSV
    csv_file = f"{args.hub}_trade_opportunities.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    print(f"\n[+] Full report exported to: {csv_file}")
    print("[+] Done.")

if __name__ == "__main__":
    main()
