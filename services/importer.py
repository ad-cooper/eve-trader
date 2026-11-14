import os
import json
import duckdb
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from services.esi import SKIE_TOKEN_FILE, get_character_auth
from services.market import (
    DUCKDB_PATH,
    SQLITE_PATH,
    REGION_THE_FORGE,
    JITA_STATION_ID,
    create_session,
    fetch_lowest_sell_order,
)

REGION_DOMAIN = 10000043
AMARR_STATION_ID = 60008494

CJ6MT_STRUCTURE_ID = 1049588174021

# Packaged ship volumes (m3) by SDE groupID
PACKAGED_SHIP_M3 = {
    25: 2500.0, 324: 2500.0, 830: 2500.0, 831: 2500.0, 834: 2500.0, 893: 2500.0, 1283: 2500.0, 1527: 2500.0,  # Frigates
    420: 5000.0, 541: 5000.0, 1305: 5000.0, 1534: 5000.0,  # Destroyers
    26: 10000.0, 358: 10000.0, 832: 10000.0, 833: 10000.0, 894: 10000.0, 906: 10000.0, 963: 10000.0, 1972: 10000.0,  # Cruisers
    419: 15000.0, 540: 15000.0, 1201: 15000.0,  # Battlecruisers
    27: 50000.0, 898: 50000.0, 900: 50000.0,  # Battleships
    28: 20000.0, 380: 20000.0, 1202: 20000.0,  # Haulers / DST / BR
    463: 3750.0, 543: 3750.0,  # Mining Barges / Exhumers
}












def fetch_structure_order_book(structure_id: int, access_token: str) -> dict:
    """Downloads all pages of a player structure's market and aggregates lowest ask, sell depth, and top buy per type_id."""
    headers = {"Authorization": f"Bearer {access_token}"}
    first = requests.get(
        f"https://esi.evetech.net/latest/markets/structures/{structure_id}/?datasource=tranquility&page=1",
        headers=headers,
        timeout=10,
    )
    first.raise_for_status()
    total_pages = int(first.headers.get("X-Pages", 1))
    all_orders = list(first.json())

    if total_pages > 1:
        def get_page(p: int):
            r = requests.get(
                f"https://esi.evetech.net/latest/markets/structures/{structure_id}/?datasource=tranquility&page={p}",
                headers=headers,
                timeout=10,
            )
            return r.json() if r.status_code == 200 else []

        with ThreadPoolExecutor(max_workers=8) as ex:
            for rows in ex.map(get_page, range(2, total_pages + 1)):
                all_orders.extend(rows)

    book = {}
    for o in all_orders:
        tid = int(o["type_id"])
        price = float(o["price"])
        vol = int(o["volume_remain"])
        entry = book.setdefault(tid, {"lowest_ask": None, "sell_vol": 0, "top_buy": None, "buy_vol": 0})
        if o.get("is_buy_order"):
            entry["buy_vol"] += vol
            if entry["top_buy"] is None or price > entry["top_buy"]:
                entry["top_buy"] = price
        else:
            entry["sell_vol"] += vol
            if entry["lowest_ask"] is None or price < entry["lowest_ask"]:
                entry["lowest_ask"] = price
    return book


def _load_sde_item_metadata(type_ids: list[int] | None = None) -> dict:
    """Returns {type_id: {'name': str, 'm3': float, 'group_name': str}} using packaged ship m3 where applicable."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
    where = ""
    if type_ids:
        id_str = ",".join(str(int(t)) for t in set(type_ids))
        where = f"AND t.typeID IN ({id_str})"

    rows = con.execute(f"""
        SELECT t.typeID, t.typeName, t.volume, t.groupID, g.groupName
        FROM sde.invTypes t
        JOIN sde.invGroups g ON t.groupID = g.groupID
        WHERE t.published = 1 AND t.marketGroupID IS NOT NULL {where}
    """).fetchall()
    con.close()

    meta = {}
    for tid, name, raw_vol, gid, gname in rows:
        m3 = PACKAGED_SHIP_M3.get(int(gid), float(raw_vol or 0.01))
        meta[int(tid)] = {"name": str(name), "m3": max(m3, 0.01), "group_name": str(gname)}
    return meta


def _get_top_jita_candidates(limit: int = 250) -> list[int]:
    """Selects high-liquidity, non-expiring Jita candidates from DuckDB for Amarr import scanning."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
    rows = con.execute(f"""
        SELECT h.type_id
        FROM forge_market_history h
        JOIN sde.invTypes t ON h.type_id = t.typeID
        WHERE h.date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history)
          AND h.volume > 0
          AND t.published = 1
          AND COALESCE(t.marketGroupID, 0) NOT IN (2487, 1661)
          AND LOWER(COALESCE(t.description, '')) NOT LIKE '%cease to function%'
          AND LOWER(COALESCE(t.description, '')) NOT LIKE '%will expire%'
        GROUP BY h.type_id
        HAVING AVG(h.volume) >= 8 AND AVG(h.average * h.volume) >= 150000000
        ORDER BY AVG(h.average * h.volume) DESC
        LIMIT {int(limit)}
    """).fetchall()
    con.close()
    return [int(r[0]) for r in rows]


def scan_amarr_imports(
    freight_isk_per_m3: float = 250.0,
    collateral_pct: float = 0.01,
    amarr_exit_fee: float = 0.0485,
    min_margin_pct: float = 8.0,
    min_daily_vol: float = 3.0,
    candidate_limit: int = 250,
) -> list[dict]:
    """Finds profitable Jita -> Amarr VIII (60008494) imports validated against live Amarr books & 30d Amarr volume."""
    tids = _get_top_jita_candidates(limit=candidate_limit)
    if not tids:
        return []

    meta = _load_sde_item_metadata(tids)
    session = create_session()

    def fetch_amarr_metrics(tid: int):
        _, jita_ask = fetch_lowest_sell_order(session, tid)
        if not jita_ask or jita_ask <= 0:
            return None

        # Live Amarr VIII lowest sell
        o_url = f"https://esi.evetech.net/latest/markets/{REGION_DOMAIN}/orders/?datasource=tranquility&order_type=sell&type_id={tid}"
        r_ord = session.get(o_url, timeout=7)
        if r_ord.status_code != 200:
            return None
        amarr_sells = [float(o["price"]) for o in r_ord.json() if o.get("location_id") == AMARR_STATION_ID]
        if not amarr_sells:
            return None
        amarr_ask = min(amarr_sells)

        # Quick pre-filter before fetching history
        if amarr_ask <= jita_ask * (1.0 + min_margin_pct / 100.0):
            return None

        # Amarr 30d regional daily volume
        h_url = f"https://esi.evetech.net/latest/markets/{REGION_DOMAIN}/history/?datasource=tranquility&type_id={tid}"
        r_hist = session.get(h_url, timeout=7)
        daily_vol = 0.0
        if r_hist.status_code == 200:
            hist = r_hist.json()[-30:]
            if hist:
                daily_vol = sum(float(d.get("volume", 0)) for d in hist) / 30.0
        if daily_vol < min_daily_vol:
            return None

        return tid, jita_ask, amarr_ask, daily_vol

    opportunities = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(fetch_amarr_metrics, tid) for tid in tids]
        for fut in as_completed(futures):
            res = fut.result()
            if not res:
                continue
            tid, jita_ask, amarr_ask, daily_vol = res
            info = meta.get(tid)
            if not info:
                continue

            m3 = info["m3"]
            freight_cost = (m3 * freight_isk_per_m3) + (jita_ask * collateral_pct)
            landed_cost = jita_ask + freight_cost
            list_price = round(amarr_ask - 0.01, 2)
            net_rev = list_price * (1.0 - amarr_exit_fee)
            profit_unit = net_rev - landed_cost
            margin_pct = (profit_unit / landed_cost) * 100.0 if landed_cost > 0 else 0.0

            if margin_pct < min_margin_pct or profit_unit <= 0:
                continue

            suggested_qty = max(1, min(int(round(daily_vol * 2.0)), int(1.5e9 // max(jita_ask, 1.0))))
            opportunities.append({
                "destination": "AMARR",
                "type_id": tid,
                "name": info["name"],
                "group": info["group_name"],
                "m3": round(m3, 2),
                "jita_ask": round(jita_ask, 2),
                "dest_ask": round(amarr_ask, 2),
                "suggested_sell": list_price,
                "freight_cost": round(freight_cost, 2),
                "landed_cost": round(landed_cost, 2),
                "profit_unit": round(profit_unit, 2),
                "margin_pct": round(margin_pct, 1),
                "isk_per_m3": round(profit_unit / m3, 1),
                "dest_daily_vol": round(daily_vol, 1),
                "suggested_qty": suggested_qty,
                "est_total_profit": round(profit_unit * suggested_qty, 2),
                "status": "UNDERCUT_LIST",
            })

    opportunities.sort(key=lambda x: x["est_total_profit"], reverse=True)
    return opportunities


def scan_cj6mt_imports(
    freight_isk_per_m3: float = 950.0,
    collateral_pct: float = 0.01,
    citadel_exit_fee: float = 0.041,
    min_margin_pct: float = 12.0,
    min_isk_per_m3: float = 1500.0,
) -> dict:
    """Scans the C-J6MT structure order book via Skie Ankora's token and compares every listed item against Jita."""
    access_token, char_id = get_character_auth(SKIE_TOKEN_FILE)
    if not access_token:
        raise RuntimeError(f'Unable to load or refresh Skie Ankora token from {SKIE_TOKEN_FILE}')
    structure_id = CJ6MT_STRUCTURE_ID
    struct_book = fetch_structure_order_book(structure_id, access_token)

    # Also include top 60 high-velocity Jita PvP/doctrine items to detect C-J6MT stockouts
    doctrine_tids = _get_top_jita_candidates(limit=60)
    all_tids = sorted(set(struct_book.keys()) | set(doctrine_tids))
    meta = _load_sde_item_metadata(all_tids)

    # Filter to valid, published, non-expiring items before querying Jita
    valid_tids = [tid for tid in all_tids if tid in meta]
    session = create_session()
    jita_asks = {}

    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(fetch_lowest_sell_order, session, tid): tid for tid in valid_tids}
        for fut in as_completed(futures):
            tid, ask = fut.result()
            if ask and ask > 0:
                jita_asks[tid] = float(ask)

    opportunities = []
    for tid, jita_ask in jita_asks.items():
        info = meta[tid]
        m3 = info["m3"]
        s_entry = struct_book.get(tid, {"lowest_ask": None, "sell_vol": 0, "top_buy": None, "buy_vol": 0})
        dest_ask = s_entry["lowest_ask"]
        sell_vol = s_entry["sell_vol"]
        top_buy = s_entry["top_buy"]

        freight_cost = (m3 * freight_isk_per_m3) + (jita_ask * collateral_pct)
        landed_cost = jita_ask + freight_cost

        if dest_ask and dest_ask > 0:
            list_price = round(dest_ask - 0.01, 2)
            status = "LOW_STOCK" if sell_vol <= 3 else "ACTIVE_BOOK"
        elif top_buy and top_buy > landed_cost * 1.15:
            list_price = round(max(top_buy * 1.15, jita_ask * 1.30), 2)
            status = "STOCKOUT_BUY_DEMAND"
        elif tid in doctrine_tids and jita_ask >= 5e6:
            list_price = round(jita_ask * 1.30, 2)
            status = "STOCKOUT_DOCTRINE"
        else:
            continue

        net_rev = list_price * (1.0 - citadel_exit_fee)
        profit_unit = net_rev - landed_cost
        margin_pct = (profit_unit / landed_cost) * 100.0 if landed_cost > 0 else 0.0
        isk_per_m3 = profit_unit / m3

        if margin_pct < min_margin_pct or isk_per_m3 < min_isk_per_m3 or profit_unit <= 0:
            continue

        suggested_qty = max(1, min(5 if status.startswith("STOCKOUT") else max(2, sell_vol), int(2.0e9 // max(jita_ask, 1.0))))
        opportunities.append({
            "destination": "C-J6MT",
            "structure_id": structure_id,
            "type_id": tid,
            "name": info["name"],
            "group": info["group_name"],
            "m3": round(m3, 2),
            "jita_ask": round(jita_ask, 2),
            "dest_ask": round(dest_ask, 2) if dest_ask else None,
            "dest_sell_vol": sell_vol,
            "dest_top_buy": round(top_buy, 2) if top_buy else None,
            "suggested_sell": list_price,
            "freight_cost": round(freight_cost, 2),
            "landed_cost": round(landed_cost, 2),
            "profit_unit": round(profit_unit, 2),
            "margin_pct": round(margin_pct, 1),
            "isk_per_m3": round(isk_per_m3, 1),
            "suggested_qty": suggested_qty,
            "est_total_profit": round(profit_unit * suggested_qty, 2),
            "status": status,
        })

    opportunities.sort(key=lambda x: (x["isk_per_m3"], x["est_total_profit"]), reverse=True)
    return {
        "structure_id": structure_id,
        "character_id": char_id,
        "total_structure_types": len(struct_book),
        "opportunities": opportunities,
    }


def get_import_suggestions(
    amarr_rate: float = 250.0,
    cj6mt_rate: float = 950.0,
    limit: int = 40,
) -> dict:
    amarr_ops = scan_amarr_imports(freight_isk_per_m3=amarr_rate)[:limit]
    cj6mt_res = scan_cj6mt_imports(freight_isk_per_m3=cj6mt_rate)
    cj6mt_ops = cj6mt_res["opportunities"][:limit]

    # Build ready-to-paste Jita MultiBuy strings for top 20 of each destination
    amarr_multibuy = "\n".join(f"{x['name']} {x['suggested_qty']}" for x in amarr_ops[:20])
    cj6mt_multibuy = "\n".join(f"{x['name']} {x['suggested_qty']}" for x in cj6mt_ops[:20])

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "amarr": {
            "station_id": AMARR_STATION_ID,
            "freight_rate_m3": amarr_rate,
            "count": len(amarr_ops),
            "multibuy_text": amarr_multibuy,
            "items": amarr_ops,
        },
        "cj6mt": {
            "structure_id": cj6mt_res["structure_id"],
            "character_id": cj6mt_res["character_id"],
            "total_structure_types": cj6mt_res["total_structure_types"],
            "freight_rate_m3": cj6mt_rate,
            "count": len(cj6mt_ops),
            "multibuy_text": cj6mt_multibuy,
            "items": cj6mt_ops,
        },
    }


if __name__ == "__main__":
    data = get_import_suggestions()
    print(f"=== AMARR IMPORTS ({data['amarr']['count']} items) ===")
    for r in data["amarr"]["items"][:10]:
        print(f"  {r['name']:<38} | Jita: {r['jita_ask']/1e6:6.1f}M -> Amarr: {r['suggested_sell']/1e6:6.1f}M | Margin: {r['margin_pct']:5.1f}% | {r['isk_per_m3']:8.0f} ISK/m3")
    print(f"\n=== C-J6MT IMPORTS (Structure {data['cj6mt']['structure_id']}, {data['cj6mt']['count']} items) ===")
    for r in data["cj6mt"]["items"][:10]:
        print(f"  {r['name']:<38} | Jita: {r['jita_ask']/1e6:6.1f}M -> C-J6: {r['suggested_sell']/1e6:6.1f}M | Margin: {r['margin_pct']:5.1f}% | {r['isk_per_m3']:8.0f} ISK/m3 | {r['status']}")
