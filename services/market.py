import requests
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.desk import Desk
from services.storage import (
    load_cached_orders,
    load_market_prices_cache,
    load_all_desk_ledgers,
    load_desk_csv,
    read_json,
    write_json
)
from services.accounting import calc_line_financials
from services.esi import (
    create_esi_session as create_session,
    extract_lowest_sell_for_station,
    fetch_station_book_top,
    fetch_wallet_transactions,
)
from services.sde import resolve_type_ids
from services.analytics_db import (
    query_historical_buys_by_names,
    query_30d_vwap_by_type_ids,
    query_earliest_buy_and_snapshot_dates
)

from constants import (
    JITA_STATION_ID,
    MARKET_CACHE_FILE
)

def load_cached_market_telemetry() -> dict:
    return read_json(MARKET_CACHE_FILE, default={})

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
            futures = {ex.submit(extract_lowest_sell_for_station, session, tid): tid for tid in still_missing}
            for fut in as_completed(futures):
                tid, ask = fut.result()
                jita_asks[tid] = float(ask) if (ask and ask > 0) else None

def update_ledger_market_prices():
    """
    Updates cached Jita 4-4 ask/buy telemetry using bulk Fuzzwork asks (100 IDs/call)
    for all ledger items, and targeted ESI order_type=all calls only for active buy orders
    or items missing from Fuzzwork.
    """
    ledgers = load_all_desk_ledgers()
    all_items = ledgers["evergreen"] + ledgers["tactical"] + ledgers["hold"]

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
            futures = {executor.submit(fetch_station_book_top, tid, session): tid for tid in esi_target_tids}
            for fut in as_completed(futures):
                tid, top_b, low_s = fut.result()
                esi_books[tid] = (float(top_b or 0.0), float(low_s or 0.0))

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

    write_json(MARKET_CACHE_FILE, prices)

    return prices

def _query_wallet_buys(type_ids: list[int]) -> dict[int, list[dict]]:
    """Groups live ESI wallet buy transactions by type_id for cost-basis and hold-duration lookups."""
    target_set = set(type_ids)
    buys_by_type: dict[int, list[dict]] = defaultdict(list)
    try:
        for tx in fetch_wallet_transactions() or []:
            if not tx.get("is_buy"):
                continue
            tid = tx.get("type_id")
            if tid not in target_set:
                continue
            dt_obj = None
            raw_d = tx.get("date")
            if raw_d:
                try:
                    dt_obj = datetime.fromisoformat(str(raw_d).replace("Z", "+00:00"))
                except Exception:
                    pass
            buys_by_type[tid].append({
                "qty": int(tx.get("quantity", 1)),
                "price": float(tx.get("unit_price", 0.0)),
                "date": dt_obj,
            })
    except Exception:
        pass
    return buys_by_type


def _query_historical_buys_db(type_ids: list[int], name_map: dict[int, str] | None = None) -> dict[int, dict]:
    """Queries DuckDB historical_wallet_buys for weighted average cost and earliest acquisition date."""
    if not type_ids:
        return {}
    resolved = name_map if name_map is not None else resolve_type_ids(type_ids)
    names = [resolved.get(tid) for tid in type_ids if resolved.get(tid)]
    if not names:
        return {}

    by_name = query_historical_buys_by_names(names)
    out: dict[int, dict] = {}
    for tid in type_ids:
        iname = resolved.get(tid)
        if iname and iname in by_name:
            out[tid] = by_name[iname]
    return out


def get_cost_basis_map(type_ids: list, history_map: dict) -> dict:
    if not type_ids:
        return {}
    cost_map = {}

    # 1. Live ESI wallet buy transactions
    wallet_buys = _query_wallet_buys(type_ids)
    for tid in type_ids:
        fills = wallet_buys.get(tid, [])
        total_q = sum(f["qty"] for f in fills)
        if total_q > 0:
            avg_p = sum(f["qty"] * f["price"] for f in fills) / total_q
            cost_map[tid] = {"unit_cost": avg_p, "source": "WALLET"}

    # 2. jEveAssets lifetime wallet buys in DuckDB
    unmatched_for_jeve = [tid for tid in type_ids if tid not in cost_map]
    if unmatched_for_jeve:
        for tid, info in _query_historical_buys_db(unmatched_for_jeve).items():
            if info["avg_price"] > 0:
                cost_map[tid] = {"unit_cost": info["avg_price"], "source": "JEVEASSETS"}

    # 3. Historical ledger snapshots (target_buy)
    for tid in type_ids:
        if tid not in cost_map and tid in history_map:
            tb = history_map[tid].get("target_buy")
            if tb and tb > 0:
                cost_map[tid] = {"unit_cost": float(tb), "source": "LEDGER"}

    # 4. Fallback: 30d VWAP from DuckDB forge_market_history
    unmatched_tids = [tid for tid in type_ids if tid not in cost_map]
    if unmatched_tids:
        vwap_by_tid = query_30d_vwap_by_type_ids(unmatched_tids)
        for tid, vwap in vwap_by_tid.items():
            if vwap and vwap > 0:
                cost_map[tid] = {"unit_cost": float(vwap), "source": "EST_VWAP"}

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



def _parse_utc_dt(raw_dt) -> datetime | None:
    if not raw_dt:
        return None
    dt_obj = raw_dt if isinstance(raw_dt, datetime) else None
    if dt_obj is None:
        try:
            dt_obj = datetime.fromisoformat(str(raw_dt)[:19])
        except Exception:
            return None
    return dt_obj.replace(tzinfo=timezone.utc) if dt_obj.tzinfo is None else dt_obj

def get_hold_desk_params(type_ids: list, name_map: dict, history_map: dict = None) -> dict:
    """Builds holding-duration parameters for Hold and Tactical desk items."""
    if not type_ids:
        return {}

    now_utc = datetime.now(timezone.utc)
    hold_params = {}

    # 1. Explicit CSV overrides
    csv_meta = {}
    for fname in ("hold_ledger.csv", "tactical_ledger.csv"):
        for r in load_desk_csv(fname):
            try:
                tid = int(r.get("type_id"))
                raw_d = str(r.get("date_acquired", "")).strip()
                date_val = raw_d if raw_d and raw_d.lower() not in ("none", "nan") else None
                raw_days = str(r.get("max_days", "")).strip()
                default_max = Desk.get_desk_profile("tactical" if "tactical" in fname else "hold").max_hold_days
                days_val = int(raw_days) if raw_days.isdigit() else default_max
                csv_meta[tid] = {"date_acquired": date_val, "max_days": days_val}
            except (ValueError, TypeError):
                continue

    # 2. Live ESI wallet transactions (earliest active fill date)
    wallet_buys = _query_wallet_buys(type_ids)
    esi_earliest_buys = {
        tid: min(f["date"] for f in fills if f["date"] is not None)
        for tid, fills in wallet_buys.items()
        if any(f["date"] is not None for f in fills)
    }

    # 3. DuckDB historical buys & ledger snapshots
    db_hist_buys = _query_historical_buys_db(type_ids, name_map=name_map)
    names_to_lookup = [name_map.get(tid) for tid in type_ids if name_map.get(tid)]
    try:
        db_clean_buys, db_snap_dates = query_earliest_buy_and_snapshot_dates(names_to_lookup, type_ids)
    except Exception:
        db_clean_buys, db_snap_dates = {}, {}

    for tid in type_ids:
        meta = csv_meta.get(tid, {})
        desk_from_hist = (history_map or {}).get(tid, {}).get("desk", "HOLD")
        max_days = meta.get("max_days", Desk.get_desk_profile(desk_from_hist).max_hold_days)
        date_acq = meta.get("date_acquired")
        item_name = name_map.get(tid, "")

        dt_parsed = None
        if date_acq:
            try:
                dt_parsed = datetime.strptime(date_acq[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                pass
        if not dt_parsed:
            dt_parsed = esi_earliest_buys.get(tid)
        if not dt_parsed and tid in db_hist_buys:
            dt_parsed = _parse_utc_dt(db_hist_buys[tid].get("first_date"))
        if not dt_parsed and item_name in db_clean_buys:
            dt_parsed = _parse_utc_dt(db_clean_buys[item_name])
        if not dt_parsed and tid in db_snap_dates:
            dt_parsed = _parse_utc_dt(db_snap_dates[tid])

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
            "is_expired": bool(days_held >= max_days and days_held > 0),
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

