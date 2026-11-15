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

from services.esi import (
    create_esi_session as create_session,
    fetch_lowest_sell_order,
    fetch_top_buy_order,
    fetch_telemetry_pair,
    resolve_primary_character,
    fetch_character_locations_stock,
)

from services.esi import (
    create_esi_session as create_session,
    fetch_lowest_sell_order,
    fetch_top_buy_order,
    fetch_telemetry_pair,
    fetch_jita_book_top,
    resolve_primary_character,
    fetch_character_locations_stock,
)

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


def update_ledger_market_prices():
    """
    Updates cached Jita 4-4 ask/buy telemetry using bulk Fuzzwork asks (100 IDs/call)
    for all ledger items, and targeted ESI order_type=all calls only for active buy orders
    or items missing from Fuzzwork.
    """
    from services.ledgers import load_desk_csv, load_cached_orders
    eg = load_desk_csv("evergreen_ledger.csv")
    tac = load_desk_csv("tactical_ledger.csv")
    hld = load_desk_csv("hold_ledger.csv")
    all_items = eg + tac + hld

    tid_set = {int(it["type_id"]) for it in all_items if it.get("type_id")}

    orders = load_cached_orders() or {}
    active_buy_tids = {int(k) for k in (orders.get("buys") or {}).keys() if str(k).isdigit()}
    active_sell_tids = {int(k) for k in (orders.get("sells") or {}).keys() if str(k).isdigit()}
    tid_set |= active_buy_tids | active_sell_tids

    if not tid_set:
        return {}

    unique_tids = sorted(tid_set)
    session = create_session()
    prev_cache = load_market_prices_cache()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # 1. Bulk-fetch lowest asks across all ledger + active items in ~2 Fuzzwork requests
    bulk_asks = fetch_station_asks_bulk(session, JITA_STATION_ID, unique_tids)

    # 2. Only hit ESI order_type=all for active buy orders (need top_buy) or uncached asks
    esi_target_tids = sorted(active_buy_tids | {tid for tid in unique_tids if tid not in bulk_asks})
    esi_books: dict[int, tuple[float, float]] = {}
    if esi_target_tids:
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_jita_book_top, tid, session): tid for tid in esi_target_tids}
            for fut in as_completed(futures):
                tid, top_b, low_s = fut.result()
                esi_books[tid] = (top_b, low_s)

    prices = {}
    for tid in unique_tids:
        prev = prev_cache.get(str(tid)) or {}
        top_b, low_s = esi_books.get(tid, (0.0, 0.0))
        ask = low_s if low_s > 0 else bulk_asks.get(tid)
        buy = top_b if top_b > 0 else None
        prices[str(tid)] = {
            "lowest_ask": ask if (ask and ask > 0) else prev.get("lowest_ask"),
            "top_buy": buy if (buy and buy > 0) else prev.get("top_buy"),
            "timestamp": now_str,
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
    from workflows.buy_orders import audit_missing_buys
    return audit_missing_buys(desk=desk_filter)
