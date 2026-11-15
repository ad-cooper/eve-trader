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

from services.ledgers import get_desk_profile

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
MARKET_CACHE_FILE = "ledgers/market_prices_cache.json"

REGION_THE_FORGE = 10000002
JITA_STATION_ID = 60003760

from services.accounting import (
    BUY_SCC_SURCHARGE as BUY_FEE_CITADEL,
    TOTAL_EXIT_FEE as EXIT_FEE_JITA,
    calc_unit_cost,
    calc_net_proceeds,
    calc_line_financials,
)

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

def fetch_station_asks_bulk(session: requests.Session, station_id: int, tids: list[int]) -> dict[int, float]:
    """Bulk-fetches lowest sell prices for any NPC station (100 IDs/request) via Fuzzwork."""
    asks: dict[int, float] = {}
    if not tids:
        return asks

    chunks = [tids[i : i + 100] for i in range(0, len(tids), 100)]

    def fetch_batch(chunk: list[int]):
        types_str = ",".join(str(x) for x in chunk)
        url = f"https://market.fuzzwork.co.uk/aggregates/?station={station_id}&types={types_str}"
        try:
            r = session.get(url, timeout=8)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for data in ex.map(fetch_batch, chunks):
            for tid_str, d in data.items():
                try:
                    tid = int(tid_str)
                    sell_min = float(d.get("sell", {}).get("min") or 0.0)
                    if sell_min > 0:
                        asks[tid] = sell_min
                except Exception:
                    pass
    return asks


def ensure_jita_asks(session: requests.Session, tids: list[int], jita_asks: dict[int, float | None]) -> None:
    """Populates `jita_asks` in place using bulk Fuzzwork lookups with ESI fallback."""
    missing = [tid for tid in tids if tid not in jita_asks]
    if not missing:
        return

    jita_asks.update(fetch_station_asks_bulk(session, JITA_STATION_ID, missing))
    still_missing = [tid for tid in missing if tid not in jita_asks]
    if still_missing:
        with ThreadPoolExecutor(max_workers=16) as ex:
            futures = {ex.submit(fetch_lowest_sell_order, session, tid): tid for tid in still_missing}
            for fut in as_completed(futures):
                tid, ask = fut.result()
                jita_asks[tid] = float(ask) if (ask and ask > 0) else None


def resolve_primary_character() -> tuple[str | None, str | None, int | None]:
    """Resolves (character_name, access_token, character_id) for the primary Jita character."""
    sso_path = "ledgers/sso_token.json"
    if not os.path.exists(sso_path):
        return None, None, None
    try:
        from services.esi import get_character_auth
        with open(sso_path, "r", encoding="utf-8") as f:
            sso = json.load(f)
        if isinstance(sso, list) and sso and isinstance(sso[0], dict):
            cname = sso[0].get("character_name")
        elif isinstance(sso, dict):
            cname = sso.get("character_name")
        else:
            cname = None
        if cname:
            tok, cid = get_character_auth(cname)
            return cname, tok, int(cid) if cid else None
    except Exception:
        pass
    return None, None, None


def fetch_character_locations_stock(
    character_id: int | None,
    access_token: str | None,
    location_ids: list[int],
) -> dict[int, dict[int, dict]]:
    """
    Fetches a character's hangar assets (walking nested container chains) and active sell orders
    across multiple `location_ids` in a single ESI pass.
    Returns {location_id: {type_id: {'hangar_qty': int, 'sell_qty': int, 'my_lowest_sell': float|None}}}.
    """
    target_locs = {int(loc) for loc in location_ids if loc}
    result: dict[int, dict[int, dict]] = {loc: {} for loc in target_locs}
    if not character_id or not access_token or not target_locs:
        return result

    headers = {"Authorization": f"Bearer {access_token}"}
    all_assets = []
    page = 1
    while True:
        try:
            r = requests.get(
                f"https://esi.evetech.net/latest/characters/{character_id}/assets/?datasource=tranquility&page={page}",
                headers=headers,
                timeout=10,
            )
            if r.status_code != 200:
                break
            rows = r.json()
            if not rows:
                break
            all_assets.extend(rows)
            total_pages = int(r.headers.get("X-Pages", 1))
            if page >= total_pages:
                break
            page += 1
        except Exception:
            break

    parent_map = {
        int(a["item_id"]): int(a["location_id"])
        for a in all_assets
        if "item_id" in a and "location_id" in a
    }

    def resolve_target_loc(loc_id: int) -> int | None:
        seen = set()
        cur = loc_id
        while cur and cur not in seen:
            if cur in target_locs:
                return cur
            seen.add(cur)
            cur = parent_map.get(cur)
        return None

    for a in all_assets:
        matched_loc = resolve_target_loc(int(a.get("location_id") or 0))
        if matched_loc is None:
            continue
        flag = str(a.get("location_flag") or "")
        if flag not in ("Hangar", "Unlocked", "AutoFit", "Cargo", "FleetHangar", "ShipHangar"):
            continue
        tid = int(a["type_id"])
        qty = int(a.get("quantity", 1))
        entry = result[matched_loc].setdefault(tid, {"hangar_qty": 0, "sell_qty": 0, "my_lowest_sell": None})
        entry["hangar_qty"] += qty

    try:
        r_ord = requests.get(
            f"https://esi.evetech.net/latest/characters/{character_id}/orders/?datasource=tranquility",
            headers=headers,
            timeout=10,
        )
        if r_ord.status_code == 200:
            for o in r_ord.json():
                loc = int(o.get("location_id") or 0)
                if loc not in target_locs or o.get("is_buy_order"):
                    continue
                tid = int(o["type_id"])
                vol = int(o.get("volume_remain", 0))
                price = float(o.get("price", 0.0))
                entry = result[loc].setdefault(tid, {"hangar_qty": 0, "sell_qty": 0, "my_lowest_sell": None})
                entry["sell_qty"] += vol
                if price > 0 and (entry["my_lowest_sell"] is None or price < entry["my_lowest_sell"]):
                    entry["my_lowest_sell"] = price
    except Exception:
        pass

    return result


def fetch_telemetry_pair(session: requests.Session, type_id: int):
    _, ask = fetch_lowest_sell_order(session, type_id)
    _, buy = fetch_top_buy_order(session, type_id)
    return type_id, ask, buy

def update_ledger_market_prices():
    from services.ledgers import load_desk_csv, get_desk_profile, load_cached_orders
    eg = load_desk_csv("evergreen_ledger.csv")
    tac = load_desk_csv("tactical_ledger.csv")
    hld = load_desk_csv("hold_ledger.csv")
    all_items = eg + tac + hld

    tid_set = {int(it["type_id"]) for it in all_items if it.get("type_id")}

    # Include active ESI buy/sell order type_ids so rotated-out incumbent rows stay polled
    orders = load_cached_orders() or {}
    for bucket in ("buys", "sells"):
        for k in (orders.get(bucket) or {}).keys():
            if str(k).isdigit():
                tid_set.add(int(k))

    if not tid_set:
        return {}

    unique_tids = list(tid_set)
    session = create_session()
    prev_cache = load_market_prices_cache()
    prices = {}

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_telemetry_pair, session, tid): tid for tid in unique_tids}
        for fut in as_completed(futures):
            tid, ask, buy = fut.result()
            prev = prev_cache.get(str(tid)) or {}
            prices[str(tid)] = {
                "lowest_ask": ask if (ask and ask > 0) else prev.get("lowest_ask"),
                "top_buy": buy if (buy and buy > 0) else prev.get("top_buy"),
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

from services.sde import (
    resolve_type_names,
    resolve_type_ids,
    get_latest_ledger_targets as get_historical_ledger_targets,
    parse_eve_item_lines as parse_multisell_export_text,
)

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

def get_active_sell_orders_map() -> dict:
    """
    Loads cached character orders and groups active sell orders by integer type_id.
    Handles dict keyed by string type_id: { "53710": {"order_id": ..., "price": ..., "volume_remain": ...} }
    Returns: { type_id_int: [ {order_id, price, volume_remain}, ... ] }
    """
    try:
        from services.ledgers import load_cached_orders
        raw = load_cached_orders()
        sells_raw = raw.get("sells", {}) if isinstance(raw, dict) else {}
        
        sells_by_tid = {}
        for k, v in sells_raw.items():
            try:
                tid = int(k)
            except (ValueError, TypeError):
                continue

            order_list = v if isinstance(v, list) else [v]
            if tid not in sells_by_tid:
                sells_by_tid[tid] = []

            for o in order_list:
                sells_by_tid[tid].append({
                    "order_id": o.get("order_id"),
                    "price": float(o.get("price", 0.0)),
                    "volume_remain": int(o.get("volume_remain", 0)),
                    "volume_total": int(o.get("volume_total", o.get("volume_remain", 0)))
                })

        return sells_by_tid
    except Exception as e:
        print(f"[!] Error building active sells map: {e}")
        return {}



def get_hold_desk_params(type_ids: list, name_map: dict, history_map: dict = None) -> dict:
    """
    Builds a lookup of holding parameters for Hold and Tactical desk items.
    Resolution order for date_acquired:
      1. Explicit CSV override (hold_ledger.csv or tactical_ledger.csv)
      2. Live ESI wallet transactions (most recent buy date)
      3. DuckDB historical_wallet_buys (MAX date)
      4. DuckDB clean_transactions (MAX buy tx_date)
      5. DuckDB ledger_history (MAX snapshot_time)
    """
    if not type_ids:
        return {}

    from datetime import datetime, timezone
    import duckdb
    from services.ledgers import load_desk_csv, get_desk_profile

    now_utc = datetime.now(timezone.utc)
    hold_params = {}

    # 1. Read CSV overrides from both hold_ledger and tactical_ledger
    csv_meta = {}
    for fname in ["hold_ledger.csv", "tactical_ledger.csv"]:
        rows = load_desk_csv(fname)
        for r in rows:
            try:
                tid = int(r.get("type_id"))
                raw_d = str(r.get("date_acquired", "")).strip()
                date_val = raw_d if raw_d and raw_d.lower() not in ("none", "nan") else None
                raw_days = str(r.get("max_days", "")).strip()
                default_max = get_desk_profile("tactical" if "tactical" in fname else "hold").max_hold_days
                days_val = int(raw_days) if raw_days.isdigit() else default_max
                csv_meta[tid] = {"date_acquired": date_val, "max_days": days_val, "desk_hint": "TACTICAL" if "tactical" in fname else "HOLD"}
            except (ValueError, TypeError):
                continue

    # 2. Query live ESI wallet transactions for most recent buy date
    esi_latest_buys = {}
    try:
        from services.esi import fetch_wallet_transactions
        txs = fetch_wallet_transactions() or []
        for tx in txs:
            if tx.get("is_buy"):
                tid = tx.get("type_id")
                raw_d = tx.get("date")
                if tid in type_ids and raw_d:
                    try:
                        clean_iso = str(raw_d).replace("Z", "+00:00")
                        d_obj = datetime.fromisoformat(clean_iso)
                        if tid not in esi_latest_buys or d_obj < esi_latest_buys[tid]:
                            esi_latest_buys[tid] = d_obj
                    except Exception:
                        pass
    except Exception as e:
        print("[!] Warning reading ESI wallet buy dates:", e)

    # 3. Query DuckDB for MAX(date) if not in live ESI buffer
    db_latest = {}
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        names_to_lookup = [name_map.get(tid) for tid in type_ids if name_map.get(tid)]
        if names_to_lookup:
            clean_names = [n.replace("'", "''") for n in names_to_lookup]
            quoted_names = ",".join("'" + cn + "'" for cn in clean_names)
            q1 = f"SELECT item_name, MIN(date) FROM historical_wallet_buys WHERE item_name IN ({quoted_names}) GROUP BY item_name"
            for row in con.execute(q1).fetchall():
                if row[1]:
                    db_latest[row[0]] = row[1]

            q_clean = f"SELECT item_name, MIN(tx_date) FROM clean_transactions WHERE tx_type = 'buy' AND item_name IN ({quoted_names}) GROUP BY item_name"
            for row in con.execute(q_clean).fetchall():
                if row[1]:
                    item_n = row[0]
                    if item_n not in db_latest or row[1] > db_latest[item_n]:
                        db_latest[item_n] = row[1]

        id_str = ",".join(str(int(t)) for t in type_ids)
        q2 = f"SELECT type_id, MIN(snapshot_time) FROM ledger_history WHERE type_id IN ({id_str}) GROUP BY type_id"
        for row in con.execute(q2).fetchall():
            if row[1]:
                db_latest[row[0]] = row[1]
        con.close()
    except Exception as e:
        print("[!] Warning reading DuckDB hold history:", e)

    for tid in type_ids:
        meta = csv_meta.get(tid, {})
        desk_from_hist = (history_map or {}).get(tid, {}).get("desk", "HOLD")
        default_days = get_desk_profile(desk_from_hist).max_hold_days
        max_days = meta.get("max_days", default_days)
        date_acq = meta.get("date_acquired")
        item_name = name_map.get(tid, "")

        dt_parsed = None
        # Priority 1: Explicit CSV override
        if date_acq:
            try:
                dt_parsed = datetime.strptime(date_acq[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                pass

        # Priority 2: Live ESI wallet transaction
        if not dt_parsed and tid in esi_latest_buys:
            dt_parsed = esi_latest_buys[tid]

        # Priority 3: DuckDB latest buy by name
        if not dt_parsed and item_name in db_latest:
            raw_dt = db_latest[item_name]
            if isinstance(raw_dt, datetime):
                dt_parsed = raw_dt
            else:
                try:
                    dt_parsed = datetime.fromisoformat(str(raw_dt)[:19])
                except Exception:
                    pass
            if dt_parsed and dt_parsed.tzinfo is None:
                dt_parsed = dt_parsed.replace(tzinfo=timezone.utc)

        # Priority 4: DuckDB latest snapshot by type_id
        if not dt_parsed and tid in db_latest:
            raw_dt = db_latest[tid]
            if isinstance(raw_dt, datetime):
                dt_parsed = raw_dt
            else:
                try:
                    dt_parsed = datetime.fromisoformat(str(raw_dt)[:19])
                except Exception:
                    pass
            if dt_parsed and dt_parsed.tzinfo is None:
                dt_parsed = dt_parsed.replace(tzinfo=timezone.utc)

        if dt_parsed:
            days_held = max(0, (now_utc - dt_parsed).days)
            date_str = dt_parsed.strftime("%Y-%m-%d")
        else:
            days_held = 0
            date_str = "Recent"

        hold_params[tid] = {
            "date_acquired": date_str,
            "days_held": days_held,
            "max_days": max_days,
            "is_expired": bool(days_held >= max_days and days_held > 0)
        }

    return hold_params

def _collect_advisor_targets(raw_text: str | None = None, force_refresh: bool = False) -> list[dict]:
    """Resolves inventory targets either from pasted MultiSell/inventory text or loose Jita/Citadel hangar assets."""
    targets: list[dict] = []
    if raw_text and raw_text.strip():
        parsed = parse_multisell_export_text(raw_text)
        name_map = resolve_type_names([p["name"] for p in parsed])
        for p in parsed:
            tid = name_map.get(p["name"])
            if tid:
                targets.append({"type_id": tid, "name": p["name"], "qty": p["qty"]})
        return targets

    assets = load_cached_assets(max_age_seconds=300, force=force_refresh)
    if not assets:
        assets = fetch_character_assets()
    if not assets:
        return targets

    container_ids = set()
    for a in assets:
        if a.get("location_type") == "item":
            container_ids.add(a.get("location_id"))
        loc = a.get("location_id")
        if (loc == JITA_STATION_ID or (loc and loc >= 1_000_000_000_000)) and a.get("is_singleton"):
            container_ids.add(a.get("item_id"))

    type_counts: dict[int, int] = {}
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
    return targets


def _resolve_collision_strategy(
    desk: str,
    qty: int,
    unit_cost: float,
    lowest_ask: float | None,
    active_orders: list[dict],
    hold_meta: dict,
) -> tuple[str, str, float, dict]:
    """Pure rule engine for hangar items that already have an active sell order on the market."""
    from services.ledgers import get_desk_profile

    active_qty = sum(o["volume_remain"] for o in active_orders)
    min_active_price = min((o["price"] for o in active_orders), default=0.0)
    total_stack_qty = active_qty + qty
    active_ratio = (active_qty / total_stack_qty) if total_stack_qty > 0 else 0.0
    is_undercut = bool(lowest_ask and lowest_ask < min_active_price)
    desk_upper = str(desk or "UNMANAGED").upper()

    collision_action = None
    collision_badge = None
    collision_reason = ""

    if desk_upper == "EVERGREEN":
        eg_min_roi = get_desk_profile("evergreen").take_profit_roi
        undercut_net = calc_net_proceeds(lowest_ask - 0.01) if lowest_ask else 0.0
        eff_cost = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
        still_profitable = (undercut_net >= eff_cost * (1.0 + eg_min_roi / 100.0)) if eff_cost > 0 else True
        if is_undercut and not still_profitable:
            action, action_type = "HOLD (MARGIN FLOOR)", "hold"
            collision_action, collision_badge = "HOLD_HANGAR", "🛡️ MARGIN FLOOR"
            collision_reason = f"Undercut {lowest_ask:,.2f} drops net margin below {eg_min_roi:.0f}%. Hold."
            rec_sell = min_active_price
        elif is_undercut:
            action, action_type = "UNDERCUT & FLIP", "flip"
            collision_action, collision_badge = "MATCH_ACTIVE", "⚡ UNDERCUT VIABLE"
            collision_reason = f"Undercutting {lowest_ask:,.2f} still clears >={eg_min_roi:.0f}% net margin. Rotate capital."
            rec_sell = round(lowest_ask - 0.01, 2)
        else:
            action, action_type = "MATCH ACTIVE ASK", "flip"
            collision_action, collision_badge = "MATCH_ACTIVE", "🟢 TOP OF BOOK"
            collision_reason = f"Active order ({active_qty}x) is top of book."
            rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else min_active_price

    elif desk_upper == "TACTICAL":
        if is_undercut:
            if active_ratio < 0.20:
                action, action_type = "CANCEL & MERGE", "flip"
                collision_action, collision_badge = "CANCEL_MERGE", "⚡ CANCEL & MERGE"
                collision_reason = f"Active stack {active_qty}x is {active_ratio*100:.1f}% (<20%). Cancel order & merge full {total_stack_qty}x."
                rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else min_active_price
            else:
                action, action_type = "MODIFY & HOLD", "hold"
                collision_action, collision_badge = "MODIFY_HOLD", "✏️ MODIFY & HOLD"
                collision_reason = f"Active stack {active_qty}x is {active_ratio*100:.1f}% (>=20%). Modify active order to {lowest_ask-0.01:,.2f}; hold {qty}x reserve."
                rec_sell = min_active_price
        else:
            action, action_type = "FLIP (TACTICAL)", "flip"
            collision_action, collision_badge = "TOP_OF_BOOK", "🟢 TOP OF BOOK"
            collision_reason = f"Active order ({active_qty}x) is top of book."
            rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else min_active_price

    else:
        if is_undercut and hold_meta.get("is_expired"):
            action, action_type = f"TIME STOP ({hold_meta.get('days_held', 0)}d)", "time_stop"
            collision_action, collision_badge = "CANCEL_MERGE", "⏱️ TIME STOP"
            collision_reason = f"Position held {hold_meta.get('days_held', 0)}d (expired). Liquidate at {lowest_ask-0.01:,.2f}."
            rec_sell = round(lowest_ask - 0.01, 2)
        elif is_undercut:
            action, action_type = "SELF-UNDERCUT RISK", "hold"
            collision_action, collision_badge = "HOLD_HANGAR", "⚠️ SELF-UNDERCUT"
            collision_reason = f"Active order exists @ {min_active_price:,.2f}. Avoid self-undercutting."
            rec_sell = min_active_price
        else:
            action, action_type = "MARKET EXIT", "flip"
            rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else min_active_price

    collision_info = {
        "has_collision": True,
        "active_qty": active_qty,
        "active_price": min_active_price,
        "active_ratio": round(active_ratio * 100, 1),
        "is_undercut": is_undercut,
        "action": collision_action,
        "badge": collision_badge,
        "reason": collision_reason,
    }
    return action, action_type, rec_sell, collision_info


def _resolve_unlisted_strategy(
    desk: str,
    unit_cost: float,
    lowest_ask: float | None,
    target_sell: float | None,
    hold_meta: dict,
) -> tuple[str, str, float]:
    """Pure rule engine for hangar items with no active sell order."""
    from services.ledgers import get_desk_profile

    if desk == "EVERGREEN":
        rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else (target_sell or 1000000.0)
        return "FAST FLIP", "flip", rec_sell

    if desk in ["TACTICAL", "HOLD"]:
        desk_cfg = get_desk_profile(desk)
        days_held = hold_meta.get("days_held", 0)
        is_expired = hold_meta.get("is_expired", False)

        effective_cost_unit = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
        net_proceeds_unit = calc_net_proceeds(lowest_ask - 0.01) if lowest_ask else 0.0
        live_roi_pct = ((net_proceeds_unit - effective_cost_unit) / effective_cost_unit * 100.0) if effective_cost_unit > 0 else 0.0
        is_profitable = (net_proceeds_unit > effective_cost_unit) if effective_cost_unit > 0 else True
        take_profit_ready = bool(
            lowest_ask
            and lowest_ask > 0.01
            and effective_cost_unit > 0
            and desk_cfg.take_profit_days > 0
            and days_held >= desk_cfg.take_profit_days
            and live_roi_pct >= desk_cfg.take_profit_roi
        )

        if lowest_ask and target_sell and lowest_ask >= target_sell:
            if is_profitable:
                return "TARGET MET", "met", round(lowest_ask - 0.01, 2)
            breakeven_sell = round((effective_cost_unit * 1.05) / (1.0 - EXIT_FEE_JITA), 2)
            return "HOLD (MARGIN COMPRESSED)", "hold", max(round(target_sell, 2), breakeven_sell)
        if is_expired:
            rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else (target_sell or 1000000.0)
            return f"TIME STOP ({days_held}d)", "time_stop", rec_sell
        if take_profit_ready:
            return f"TAKE PROFIT ({days_held}d)", "met", round(lowest_ask - 0.01, 2)
        if target_sell:
            label = f"HOLD CYCLE ({days_held}d)" if desk == "HOLD" else "HOLD CYCLE"
            return label, "hold", round(target_sell, 2)

        rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else 1000000.0
        return "FAST FLIP", "flip", rec_sell

    rec_sell = round(lowest_ask - 0.01, 2) if (lowest_ask and lowest_ask > 0.01) else (unit_cost * 1.05 if unit_cost > 0 else 1000000.0)
    return "MARKET EXIT", "unmanaged", rec_sell


def _evaluate_advisor_item(target: dict, ctx: dict) -> dict:
    """Evaluates a single inventory target against live market asks, cost basis, active sells, and hold timers."""
    tid = target["type_id"]
    name = target["name"]
    qty = target["qty"]
    lowest_ask = ctx["lowest_asks"].get(tid)
    hist = ctx["history_map"].get(tid)
    cost_info = ctx["cost_basis_map"].get(tid, {"unit_cost": 0.0, "source": "UNKNOWN"})
    unit_cost = cost_info.get("unit_cost", 0.0)
    cost_source = cost_info.get("source", "UNKNOWN")
    desk = hist["desk"] if hist else "UNMANAGED"
    target_sell = hist["target_sell"] if hist else None
    hold_meta = ctx["hold_desk_map"].get(tid, {})
    active_orders = ctx["active_sells_map"].get(tid, [])

    if active_orders:
        action, action_type, rec_sell, collision_info = _resolve_collision_strategy(
            desk, qty, unit_cost, lowest_ask, active_orders, hold_meta
        )
    else:
        action, action_type, rec_sell = _resolve_unlisted_strategy(
            desk, unit_cost, lowest_ask, target_sell, hold_meta
        )
        collision_info = {
            "has_collision": False,
            "active_qty": 0,
            "active_price": 0.0,
            "active_ratio": 0.0,
            "is_undercut": False,
            "action": None,
            "badge": None,
            "reason": "",
        }

    line_value = rec_sell * qty
    effective_cost_unit = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
    line_cost = effective_cost_unit * qty
    profit_unit = calc_net_proceeds(rec_sell) - effective_cost_unit
    line_profit = profit_unit * qty
    margin_pct = (profit_unit / effective_cost_unit * 100.0) if effective_cost_unit > 0 else 0.0

    return {
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
        "hold_data": hold_meta,
        "cost_source": cost_source,
        "collision": collision_info,
    }


def _summarize_advisor_results(items_result: list[dict]) -> dict:
    """Aggregates portfolio totals and MultiSell clipboard strings from evaluated advisor items."""
    items_result.sort(key=lambda x: x["line_value"], reverse=True)
    multisell_lines, ready_lines = [], []
    total_value = total_cost = total_profit = 0.0
    ready_value = ready_cost = ready_profit = 0.0
    hold_value = hold_cost = hold_profit = 0.0

    for it in items_result:
        total_value += it["line_value"]
        total_cost += it["line_cost"]
        total_profit += it["profit"]
        multisell_lines.append(f"{it['name']}\t{it['recommended_sell']:.2f}")

        if it["action_type"] in ["flip", "met", "unmanaged", "time_stop"]:
            ready_value += it["line_value"]
            ready_cost += it["line_cost"]
            ready_profit += it["profit"]
            ready_lines.append(f"{it['name']}\t{it['recommended_sell']:.2f}")
        else:
            hold_value += it["line_value"]
            hold_cost += it["line_cost"]
            hold_profit += it["profit"]

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
        "hold_profit": round(hold_profit, 2),
    }


def get_live_multisell_prices(raw_text: str = None, force_refresh: bool = False):
    targets = _collect_advisor_targets(raw_text=raw_text, force_refresh=force_refresh)
    if not targets:
        return _summarize_advisor_results([])

    session = create_session()
    type_ids = [t["type_id"] for t in targets]
    lowest_asks = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_lowest_sell_order, session, tid): tid for tid in type_ids}
        for fut in as_completed(futures):
            tid, lowest_ask = fut.result()
            lowest_asks[tid] = lowest_ask

    history_map = get_historical_ledger_targets(type_ids)
    ctx = {
        "lowest_asks": lowest_asks,
        "history_map": history_map,
        "cost_basis_map": get_cost_basis_map(type_ids, history_map),
        "active_sells_map": get_active_sell_orders_map(),
        "hold_desk_map": get_hold_desk_params(type_ids, {t["type_id"]: t["name"] for t in targets}, history_map),
    }
    evaluated = [_evaluate_advisor_item(t, ctx) for t in targets]
    return _summarize_advisor_results(evaluated)


def evaluate_deal_opportunity(meta: dict, sells: list[tuple[float, int]], top_buy: float | None) -> dict | None:
    """
    Pure Deal Sniper classifier (INSTANT ARB, FIRESALE DUMP, DEEP DISCOUNT) with cluster-depth
    and 30-day median guardrails.
    """
    if not sells:
        return None
    tid = int(meta["type_id"])
    lowest_ask, qty_available = sells[0]
    median_30d = float(meta.get("median_30d", 0.0))

    cluster_qty = sum(q for p, q in sells if p <= (lowest_ask * 1.035))
    if cluster_qty > 3 and (lowest_ask * cluster_qty) > 300_000_000:
        return None

    wall_ask = next((p for p, _ in sells[1:] if p >= (lowest_ask * 1.05)), None)

    deal_type = None
    target_exit = None
    if top_buy and lowest_ask <= top_buy:
        deal_type = "INSTANT ARB"
        target_exit = top_buy
    elif wall_ask and wall_ask > lowest_ask:
        realistic_exit = min(wall_ask * 0.9999, median_30d * 1.05) if median_30d > 0 else (wall_ask * 0.9999)
        if realistic_exit > (lowest_ask * 1.08):
            deal_type = "FIRESALE DUMP"
            target_exit = realistic_exit
    elif median_30d > 0 and lowest_ask <= (median_30d * 0.85) and cluster_qty <= 2:
        deal_type = "DEEP DISCOUNT"
        target_exit = median_30d * 0.98

    if not deal_type or not target_exit:
        return None

    eval_qty = min(int(qty_available), 5)
    if (lowest_ask * eval_qty) > 1_500_000_000 and eval_qty > 1:
        eval_qty = max(1, int(1_500_000_000 // lowest_ask))

    fin = calc_line_financials(lowest_ask, target_exit, qty=eval_qty)
    net_margin = fin["net_margin"]
    est_profit = fin["profit"]
    total_cost = lowest_ask * eval_qty

    if net_margin < 5.0 or est_profit < 5_000_000:
        return None

    discount_pct = ((target_exit - lowest_ask) / target_exit) * 100.0
    return {
        "deal_type": deal_type,
        "type_id": tid,
        "name": meta["name"],
        "item_name": meta["name"],
        "qty": eval_qty,
        "quantity": eval_qty,
        "discount_pct": round(discount_pct, 1),
        "ask_price": round(lowest_ask, 2),
        "target_exit": round(target_exit, 2),
        "target_sell": round(target_exit, 2),
        "net_margin": round(net_margin, 1),
        "capital": round(total_cost, 2),
        "total_cost": round(total_cost, 2),
        "profit": round(est_profit, 2),
        "est_profit": round(est_profit, 2),
    }

def get_live_missing_buys(desk_filter: str = "all"):
    from services.ledgers import get_missing_buy_orders
    return get_missing_buy_orders(desk=desk_filter)
