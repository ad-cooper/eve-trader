import os
import re
import json
import duckdb
import requests
from datetime import datetime, timezone
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from services.esi import load_cached_assets, fetch_character_assets

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
MARKET_CACHE_FILE = "ledgers/market_prices_cache.json"

REGION_THE_FORGE = 10000002
JITA_STATION_ID = 60003760

BUY_FEE_CITADEL = 0.0050
EXIT_FEE_JITA = 0.04605

def create_session():
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retries)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "EVE-Desk-MarketEngine/2.0"})
    return session

def fetch_lowest_sell_order(session: requests.Session, type_id: int):
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=sell&type_id={type_id}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code == 200:
            orders = [o for o in r.json() if o.get("location_id") == JITA_STATION_ID]
            if orders:
                return type_id, min(o["price"] for o in orders)
    except Exception:
        pass
    return type_id, None

def fetch_top_buy_order(session: requests.Session, type_id: int):
    url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=buy&type_id={type_id}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code == 200:
            orders = r.json()
            if not orders:
                return type_id, None
            competing = []
            for o in orders:
                loc = o.get("location_id")
                rng = str(o.get("range", "")).lower()
                
                # 1. Direct Jita 4-4 station order
                if loc == JITA_STATION_ID:
                    competing.append(o["price"])
                # 2. Region-wide order
                elif rng == "region":
                    competing.append(o["price"])
                # 3. Upwell Citadel in Jita/Perimeter hub covering Jita 4-4
                elif loc and loc >= 1_000_000_000_000 and rng in ["1", "2", "3", "4", "5", "region"]:
                    competing.append(o["price"])

            if competing:
                return type_id, max(competing)
            return type_id, max(o["price"] for o in orders)
    except Exception:
        pass
    return type_id, None

def fetch_telemetry_pair(session: requests.Session, type_id: int):
    _, ask = fetch_lowest_sell_order(session, type_id)
    _, buy = fetch_top_buy_order(session, type_id)
    return type_id, ask, buy

def update_ledger_market_prices():
    from services.ledgers import load_desk_csv
    eg = load_desk_csv("evergreen_ledger.csv")
    tac = load_desk_csv("tactical_ledger.csv")
    hld = load_desk_csv("hold_ledger.csv")
    all_items = eg + tac + hld
    if not all_items:
        return {}

    unique_tids = list({int(it["type_id"]) for it in all_items if it.get("type_id")})
    session = create_session()
    prices = {}

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_telemetry_pair, session, tid): tid for tid in unique_tids}
        for fut in as_completed(futures):
            tid, ask, buy = fut.result()
            prices[str(tid)] = {
                "lowest_ask": ask,
                "top_buy": buy,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            }

    os.makedirs("ledgers", exist_ok=True)
    with open(MARKET_CACHE_FILE, "w") as f:
        json.dump(prices, f, indent=2)

    return prices

def load_market_prices_cache():
    if os.path.exists(MARKET_CACHE_FILE):
        try:
            with open(MARKET_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def resolve_type_names(names: list) -> dict:
    if not names:
        return {}
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
        escaped = [n.replace("'", "''") for n in set(names)]
        quoted = ",".join(f"'{x}'" for x in escaped)
        res = con.execute(f"SELECT typeID, typeName FROM sde.invTypes WHERE typeName IN ({quoted})").fetchall()
        con.close()
        return {r[1]: r[0] for r in res}
    except Exception:
        return {}

def resolve_type_ids(type_ids: list) -> dict:
    if not type_ids:
        return {}
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
        id_str = ",".join(str(int(t)) for t in set(type_ids))
        res = con.execute(f"SELECT typeID, typeName FROM sde.invTypes WHERE typeID IN ({id_str})").fetchall()
        con.close()
        return {r[0]: r[1] for r in res}
    except Exception:
        return {}

def parse_multisell_export_text(raw_text: str):
    items = []
    lines = [l.strip() for l in raw_text.strip().split("\n") if l.strip()]
    for line in lines:
        if line.lower().startswith("item") or line.lower().startswith("name"):
            continue
        if "\t" in line:
            parts = line.split("\t")
            name = parts[0].strip()
            qty = 1
            if len(parts) >= 2:
                clean_q = re.sub(r"[^\d]", "", parts[1])
                if clean_q:
                    qty = int(clean_q)
            items.append({"name": name, "qty": qty})
        else:
            match = re.match(r"^(.*?)(?:,|\s+)+(\d+)\s*$", line)
            if match:
                items.append({"name": match.group(1).strip(), "qty": int(match.group(2))})
            elif line.strip():
                items.append({"name": line.strip(), "qty": 1})
    return items

def get_historical_ledger_targets(type_ids: list) -> dict:
    if not type_ids:
        return {}
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        has_table = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'ledger_history'").fetchone()[0]
        if has_table == 0:
            con.close()
            return {}
        id_str = ",".join(str(int(t)) for t in set(type_ids))
        query = f"""
            WITH RankedHistory AS (
                SELECT 
                    type_id,
                    name,
                    desk_type,
                    target_buy,
                    target_sell,
                    snapshot_time,
                    ROW_NUMBER() OVER(PARTITION BY type_id ORDER BY snapshot_time DESC) as rn
                FROM ledger_history
                WHERE type_id IN ({id_str})
            )
            SELECT type_id, name, desk_type, target_buy, target_sell 
            FROM RankedHistory 
            WHERE rn = 1;
        """
        res = con.execute(query).fetchall()
        con.close()
        return {
            r[0]: {
                "name": r[1],
                "desk": str(r[2]).upper(),
                "target_buy": float(r[3]),
                "target_sell": float(r[4])
            }
            for r in res
        }
    except Exception:
        return {}

def get_cost_basis_map(type_ids: list, history_map: dict) -> dict:
    if not type_ids:
        return {}
    cost_map = {}

    # 1. From character wallet buy transactions (exact executed fill price)
    try:
        from services.esi import fetch_wallet_transactions
        txs = fetch_wallet_transactions()
        if txs:
            buys_by_type = defaultdict(list)
            for tx in txs:
                if tx.get("is_buy"):
                    buys_by_type[tx["type_id"]].append((tx.get("quantity", 1), tx.get("unit_price", 0.0)))
            for tid in type_ids:
                if tid in buys_by_type:
                    total_q = sum(q for q, p in buys_by_type[tid])
                    if total_q > 0:
                        avg_p = sum(q * p for q, p in buys_by_type[tid]) / total_q
                        cost_map[tid] = {"unit_cost": avg_p, "source": "WALLET"}
    except Exception:
        pass

    # 2. From jEveAssets lifetime wallet buys in DuckDB (exact historical executed fill)
    unmatched_for_jeve = [tid for tid in type_ids if tid not in cost_map]
    if unmatched_for_jeve:
        try:
            name_map = resolve_type_ids(unmatched_for_jeve)
            name_to_tid = {v: k for k, v in name_map.items() if v}
            if name_to_tid:
                con = duckdb.connect(DUCKDB_PATH, read_only=True)
                quoted_names = ",".join("'" + n.replace("'", "''") + "'" for n in name_to_tid.keys())
                jeve_query = f"""
                WITH ranked AS (
                    SELECT 
                        item_name,
                        price,
                        quantity,
                        date,
                        ROW_NUMBER() OVER (PARTITION BY item_name ORDER BY date DESC) as rn
                    FROM historical_wallet_buys
                    WHERE item_name IN ({quoted_names})
                )
                SELECT 
                    item_name, 
                    ROUND(SUM(price * quantity) / SUM(quantity), 2) as avg_price,
                    MAX(date) as last_date
                FROM ranked
                WHERE rn <= 10
                GROUP BY item_name;
                """
                jeve_rows = con.execute(jeve_query).fetchall()
                con.close()
                for r in jeve_rows:
                    iname, avg_p, last_dt = r[0], r[1], r[2]
                    tid = name_to_tid.get(iname)
                    if tid and avg_p and avg_p > 0:
                        cost_map[tid] = {"unit_cost": float(avg_p), "source": "JEVEASSETS"}
        except Exception:
            pass

    # 3. From historical ledger snapshots (target_buy)
    for tid in type_ids:
        if tid not in cost_map and tid in history_map:
            tb = history_map[tid].get("target_buy")
            if tb and tb > 0:
                cost_map[tid] = {"unit_cost": float(tb), "source": "LEDGER"}

    # 4. Fallback: 30d VWAP from DuckDB forge_market_history for unmatched items
    unmatched_tids = [tid for tid in type_ids if tid not in cost_map]
    if unmatched_tids:
        try:
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            id_str = ",".join(str(int(t)) for t in set(unmatched_tids))
            query = f"""
                SELECT 
                    type_id, 
                    SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap
                FROM forge_market_history
                WHERE type_id IN ({id_str}) AND date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history)
                GROUP BY type_id;
            """
            vwap_rows = con.execute(query).fetchall()
            con.close()
            for r in vwap_rows:
                if r[1] and r[1] > 0:
                    cost_map[r[0]] = {"unit_cost": float(r[1]), "source": "EST_VWAP"}
        except Exception:
            pass

    # 5. Unknown fallback
    for tid in type_ids:
        if tid not in cost_map:
            cost_map[tid] = {"unit_cost": 0.0, "source": "UNKNOWN"}

    return cost_map

def get_live_multisell_prices(raw_text: str = None, force_refresh: bool = False):
    targets = []
    if raw_text and raw_text.strip():
        parsed = parse_multisell_export_text(raw_text)
        name_map = resolve_type_names([p["name"] for p in parsed])
        for p in parsed:
            tid = name_map.get(p["name"])
            if tid:
                targets.append({"type_id": tid, "name": p["name"], "qty": p["qty"]})
    else:
        assets = load_cached_assets(max_age_seconds=300, force=force_refresh)
        if not assets:
            assets = fetch_character_assets()

        if assets:
            container_ids = set()
            for a in assets:
                if a.get("location_type") == "item":
                    container_ids.add(a.get("location_id"))
                loc = a.get("location_id")
                if (loc == JITA_STATION_ID or (loc and loc >= 1_000_000_000_000)) and a.get("is_singleton"):
                    container_ids.add(a.get("item_id"))

            type_counts = {}
            for a in assets:
                loc = a.get("location_id")
                loc_type = a.get("location_type")
                loc_flag = a.get("location_flag")

                is_hub = (loc == JITA_STATION_ID) or (loc is not None and loc >= 1_000_000_000_000)
                if not is_hub:
                    continue

                if loc_flag not in ["Hangar", "Deliveries"] or loc_type not in ["station", "other"]:
                    continue

                if loc_type == "item" or loc in container_ids or a.get("is_singleton"):
                    continue

                tid = a["type_id"]
                type_counts[tid] = type_counts.get(tid, 0) + a.get("quantity", 1)

            name_map = resolve_type_ids(list(type_counts.keys()))
            for tid, qty in type_counts.items():
                targets.append({"type_id": tid, "name": name_map.get(tid, f"Type {tid}"), "qty": qty})

    if not targets:
        return {
            "multisell_text": "",
            "ready_multisell_text": "",
            "items": [],
            "total_items": 0,
            "total_value": 0.0,
            "total_cost": 0.0,
            "total_profit": 0.0,
            "ready_value": 0.0,
            "ready_cost": 0.0,
            "ready_profit": 0.0,
            "hold_value": 0.0,
            "hold_cost": 0.0,
            "hold_profit": 0.0
        }

    session = create_session()
    type_ids = [t["type_id"] for t in targets]
    lowest_asks = {}

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_lowest_sell_order, session, tid): tid for tid in type_ids}
        for fut in as_completed(futures):
            tid, lowest_ask = fut.result()
            lowest_asks[tid] = lowest_ask

    history_map = get_historical_ledger_targets(type_ids)
    cost_basis_map = get_cost_basis_map(type_ids, history_map)

    items_result = []
    multisell_lines = []
    ready_lines = []
    
    total_value = 0.0
    total_cost = 0.0
    total_profit = 0.0
    
    ready_value = 0.0
    ready_cost = 0.0
    ready_profit = 0.0
    
    hold_value = 0.0
    hold_cost = 0.0
    hold_profit = 0.0

    for t in targets:
        tid = t["type_id"]
        name = t["name"]
        qty = t["qty"]
        lowest_ask = lowest_asks.get(tid)
        hist = history_map.get(tid)
        cost_info = cost_basis_map.get(tid, {"unit_cost": 0.0, "source": "UNKNOWN"})
        unit_cost = cost_info.get("unit_cost", 0.0)
        cost_source = cost_info.get("source", "UNKNOWN")

        desk = hist["desk"] if hist else "UNMANAGED"
        target_sell = hist["target_sell"] if hist else None

        # Suggested Sell Price & Action Logic
        if desk == "EVERGREEN":
            action = "FAST FLIP"
            action_type = "flip"
            rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else (target_sell or 1000000.0)
        elif desk in ["TACTICAL", "HOLD"]:
            if lowest_ask and target_sell and lowest_ask >= target_sell:
                action = "TARGET MET"
                action_type = "met"
                rec_sell = round(lowest_ask - 0.01, 2)
            elif target_sell:
                action = "HOLD CYCLE"
                action_type = "hold"
                rec_sell = round(target_sell, 2)
            else:
                action = "FAST FLIP"
                action_type = "flip"
                rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else 1000000.0
        else:
            action = "MARKET EXIT"
            action_type = "unmanaged"
            rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else (unit_cost * 1.05 if unit_cost > 0 else 1000000.0)

        # Financial Calculations (Proceeds vs Costs)
        line_value = rec_sell * qty
        effective_cost_unit = unit_cost * (1.0 + BUY_FEE_CITADEL) if unit_cost > 0 else 0.0
        line_cost = effective_cost_unit * qty
        net_proceeds_unit = rec_sell * (1.0 - EXIT_FEE_JITA)
        profit_unit = net_proceeds_unit - effective_cost_unit
        line_profit = profit_unit * qty
        margin_pct = (profit_unit / effective_cost_unit * 100.0) if effective_cost_unit > 0 else 0.0

        total_value += line_value
        total_cost += line_cost
        total_profit += line_profit

        if action_type in ["flip", "met", "unmanaged"]:
            ready_value += line_value
            ready_cost += line_cost
            ready_profit += line_profit
            ready_lines.append(f"{name}\t{rec_sell:.2f}")
        else:
            hold_value += line_value
            hold_cost += line_cost
            hold_profit += line_profit

        multisell_lines.append(f"{name}\t{rec_sell:.2f}")

        items_result.append({
            "type_id": tid,
            "name": name,
            "qty": qty,
            "desk": desk,
            "target_sell": target_sell,
            "lowest_ask": lowest_ask,
            "action": action,
            "action_type": action_type,
            "recommended_sell": rec_sell,
            "line_value": round(line_value, 2),
            "unit_cost": round(unit_cost, 2),
            "line_cost": round(line_cost, 2),
            "profit": round(line_profit, 2),
            "margin_pct": round(margin_pct, 1),
            "cost_source": cost_source
        })

    items_result.sort(key=lambda x: x["line_value"], reverse=True)
    return {
        "multisell_text": "\n".join(multisell_lines),
        "ready_multisell_text": "\n".join(ready_lines),
        "items": items_result,
        "total_items": len(items_result),
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_profit": round(total_profit, 2),
        "ready_value": round(ready_value, 2),
        "ready_cost": round(ready_cost, 2),
        "ready_profit": round(ready_profit, 2),
        "hold_value": round(hold_value, 2),
        "hold_cost": round(hold_cost, 2),
        "hold_profit": round(hold_profit, 2)
    }

def get_live_missing_buys(desk_filter: str = "all"):
    from services.ledgers import get_annotated_desks
    eg, tac, hld = get_annotated_desks()
    if desk_filter == "evergreen": pool = eg
    elif desk_filter == "tactical": pool = tac
    elif desk_filter == "hold": pool = hld
    else: pool = eg + tac + hld

    unplaced = [item for item in pool if item.get("char_status") == "UNPLACED"]
    dedup = {}
    for it in unplaced:
        tid = it["type_id"]
        if tid not in dedup: dedup[tid] = dict(it)
        else:
            dedup[tid]["qty"] += it["qty"]
            dedup[tid]["capital"] += it["capital"]
            dedup[tid]["profit"] += it["profit"]

    missing_items = list(dedup.values())
    if not missing_items:
        return {"multibuy_text": "", "items": [], "total_items": 0, "total_escrow": 0.0}

    session = create_session()
    type_ids = [it["type_id"] for it in missing_items]
    live_buys = {}

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_top_buy_order, session, tid): tid for tid in type_ids}
        for fut in as_completed(futures):
            tid, top_price = fut.result()
            live_buys[tid] = top_price

    results = []
    multibuy_lines = []
    total_escrow = 0.0

    for it in missing_items:
        tid = it["type_id"]
        name = it["name"]
        qty = int(it["qty"])
        target_buy = float(it.get("target_buy", 0.0))
        target_sell = float(it.get("target_sell", 0.0))
        top_buy = live_buys.get(tid)

        rec_buy = round(top_buy + 0.01, 2) if top_buy else round(target_buy, 2)
        top_buy = top_buy or target_buy

        cost_unit = rec_buy * (1.0 + BUY_FEE_CITADEL)
        net_proceeds = target_sell * (1.0 - EXIT_FEE_JITA)
        profit_unit = net_proceeds - cost_unit
        margin_pct = (profit_unit / cost_unit) * 100.0 if cost_unit > 0 else 0.0

        min_desk_margin = 1.085 if desk_filter == "tactical" else (1.050 if desk_filter == "evergreen" else 1.040)
        max_profitable_buy = (net_proceeds / min_desk_margin) / (1.0 + BUY_FEE_CITADEL)
        squeezed = False
        if rec_buy > max_profitable_buy:
            squeezed = True
            rec_buy = round(max_profitable_buy, 2)
            cost_unit = rec_buy * (1.0 + BUY_FEE_CITADEL)
            margin_pct = ((net_proceeds - cost_unit) / cost_unit) * 100.0

        line_escrow = rec_buy * qty
        total_escrow += line_escrow

        multibuy_lines.append(f"{name} {qty}")
        results.append({
            "type_id": tid,
            "name": name,
            "qty": qty,
            "top_buy": top_buy,
            "recommended_buy": rec_buy,
            "target_sell": target_sell,
            "margin_pct": round(margin_pct, 1),
            "line_escrow": round(line_escrow, 2),
            "squeezed": squeezed
        })

    results.sort(key=lambda x: x["line_escrow"], reverse=True)
    return {
        "multibuy_text": "\n".join(multibuy_lines),
        "items": results,
        "total_items": len(results),
        "total_escrow": round(total_escrow, 2)
    }
