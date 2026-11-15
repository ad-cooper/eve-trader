import os
import json
import pandas as pd
from services.esi import get_14d_average_volume
from services.accounting import (
    BUY_SCC_SURCHARGE,
    TOTAL_EXIT_FEE,
    calc_unit_cost,
    calc_net_proceeds,
    calc_max_profitable_buy,
    calc_projected_margin,
    calc_line_financials,
)

LEDGER_DIR = "ledgers"
ORDERS_CACHE_FILE = "ledgers/character_orders.json"
SNIPER_DEALS_FILE = "ledgers/sniper_deals.json"
MARKET_CACHE_FILE = "ledgers/market_prices_cache.json"

BUY_FEE_CITADEL = BUY_SCC_SURCHARGE
TOTAL_EXIT_FEES = TOTAL_EXIT_FEE

def load_desk_csv(filename: str):
    path = os.path.join(LEDGER_DIR, filename)
    if not os.path.exists(path):
        return []
    try:
        return pd.read_csv(path).to_dict(orient="records")
    except Exception:
        return []

def load_cached_orders():
    if os.path.exists(ORDERS_CACHE_FILE):
        try:
            with open(ORDERS_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"buys": {}, "sells": {}, "count": 0, "total_escrow": 0.0}

def load_sniper_deals():
    target = "ledgers/sniper_deals.json"
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else data.get("deals", [])
        except Exception:
            pass
    return []

def load_market_prices_cache():
    if os.path.exists(MARKET_CACHE_FILE):
        try:
            with open(MARKET_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def resolve_target_prices(item: dict, desk_type: str, live_ask: float, live_top_buy: float, sells: dict) -> tuple:
    """Determines target buy, target sell, and recommended quantity per desk strategy."""
    csv_buy = float(item.get("target_buy", 0.0))
    csv_sell = float(item.get("target_sell", 0.0))
    tid = str(item.get("type_id", ""))
    qty = int(item.get("qty", 1))

    if desk_type == "evergreen":
        t_sell = min(live_ask - 0.01, csv_sell * 1.15) if (live_ask and live_ask > 0) else csv_sell
        max_buy = calc_max_profitable_buy(t_sell, min_margin_pct=5.0)
        t_buy = min(live_top_buy + 0.01, max_buy) if (live_top_buy and live_top_buy > 0) else min(csv_buy, max_buy)
        ceil_guard = (live_ask * 0.98) if live_ask else (t_sell * 0.98)
        t_buy = min(t_buy, ceil_guard)

    elif desk_type == "tactical":
        t_sell = (0.50 * (live_ask - 0.01)) + (0.50 * csv_sell) if (live_ask and live_ask > 0) else csv_sell
        max_buy = calc_max_profitable_buy(t_sell, min_margin_pct=8.5)
        t_buy = min(live_top_buy + 0.01, max_buy) if (live_top_buy and live_top_buy > 0) else min(csv_buy, max_buy)
        ceil_guard = (live_ask * 0.95) if live_ask else (t_sell * 0.95)
        t_buy = min(t_buy, ceil_guard)

    else:  # hold
        t_sell = (live_ask - 0.01) if (tid in sells and live_ask and live_ask > csv_sell) else csv_sell
        t_buy = csv_buy
        if live_top_buy and live_top_buy > 0:
            t_buy = min(live_top_buy + 0.01, t_buy)
        if live_ask and live_ask > 0:
            t_buy = min(t_buy, live_ask * 0.88)
        cost_unit = calc_unit_cost(t_buy)
        if tid in sells:
            qty = int(item.get("qty", 1))
        else:
            qty = max(1, int(round(1_500_000_000.0 / cost_unit))) if cost_unit > 0 else 1

    return round(t_buy, 2), round(t_sell, 2), qty

def evaluate_order_action(desk_type: str, my_price: float, top_buy: float, t_sell: float, dump_freq: float, proximity_pct: float = None) -> tuple:
    """Evaluates whether an active order is winning, viable to bump, or needs cancellation."""
    min_margin = 4.5 if desk_type == "evergreen" else (18.0 if desk_type == "hold" else 8.5)
    cancel_floor = 2.5 if desk_type == "evergreen" else (10.0 if desk_type == "hold" else 5.0)
    max_buy = calc_max_profitable_buy(t_sell, min_margin)
    max_hold_buy = calc_max_profitable_buy(t_sell, cancel_floor)

    # Guard: Flag for cancellation only if margin compresses below the desk's sunk-fee cancel floor
    if max_hold_buy > 0 and my_price > (max_hold_buy * 1.005):
        cur_margin = calc_projected_margin(my_price, t_sell)
        return "CANCEL_OVERPRICED", f"OVERBID ({cur_margin:.1f}% MARGIN - CANCEL)"

    if desk_type == "hold" and proximity_pct is not None and proximity_pct > 92.0:
        return "CANCEL_RECOVERED", "DIP RECOVERED (CANCEL)"

    if not top_buy or top_buy <= 0:
        return "PATIENT", "AWAITING QUOTE"

    if my_price >= (top_buy - 0.01):
        return "TOP_BOOK", "TOP BID (WINNING)"

    next_bid = top_buy + 0.01
    proj_margin = calc_projected_margin(next_bid, t_sell)

    if top_buy <= max_buy and proj_margin >= min_margin:
        return "BUMP_VIABLE", f"BUMP VIABLE (+{proj_margin:.1f}%)"
    if desk_type == "tactical" and dump_freq >= 60.0:
        return "PATIENT", "HOLD FIRM (WAIT DUMP)"
    if desk_type == "hold":
        return "PATIENT", "HOLD FIRM (WAIT DUMP)"

    return "CANCEL_CEILING", "CEILING REACHED (CANCEL)"

def annotate_desk_items(items: list, desk_type: str, market_prices: dict, buys: dict, sells: dict) -> list:
    for item in items:
        tid = str(item.get("type_id", ""))
        p_info = market_prices.get(tid) or {}
        live_ask = p_info.get("lowest_ask")
        live_top_buy = p_info.get("top_buy")
        item["live_ask"] = live_ask
        item["ask_polled"] = tid in market_prices

        t_buy, t_sell, qty = resolve_target_prices(item, desk_type, live_ask, live_top_buy, sells)
        item["target_buy"] = t_buy
        item["target_sell"] = t_sell
        item["qty"] = qty

        # Centralized accounting math
        financials = calc_line_financials(t_buy, t_sell, qty)
        item.update(financials)

        if live_ask and t_sell > 0:
            item["proximity_pct"] = round((live_ask / t_sell) * 100.0, 1)
            item["diff_pct"] = round(((live_ask - t_sell) / t_sell) * 100.0, 1)
        else:
            item["proximity_pct"] = None
            item["diff_pct"] = 0.0

        if tid in buys:
            b = buys[tid]
            item["char_status"] = "BUY_ACTIVE"
            item["my_price"] = b["price"]
            item["my_qty"] = b["volume_remain"]
            item["escrow"] = float(b.get("escrow", item.get("capital", 0.0)))
            item["price_synced"] = abs(b["price"] - t_buy) / t_buy < 0.01 if t_buy > 0 else True
            dump_f = float(item.get("dump_freq", 0.0))
            code, label = evaluate_order_action(desk_type, b["price"], live_top_buy or 0.0, t_sell, dump_f, item.get("proximity_pct"))
            item["action_code"] = code
            item["action_text"] = label
        elif tid in sells:
            s = sells[tid]
            item["char_status"] = "SELL_ACTIVE"
            item["my_price"] = s["price"]
            item["my_qty"] = s["volume_remain"]
            item["price_synced"] = True
            item["action_code"] = "SELL_ACTIVE"
            item["action_text"] = "SELL LISTED"
        else:
            item["char_status"] = "UNPLACED"
            item["escrow"] = 0.0
            item["my_price"] = None
            item["my_qty"] = 0
            item["price_synced"] = False

            if desk_type == "hold":
                prox = item.get("proximity_pct")
                margin = item.get("net_margin", 0.0)
                cost_unit = item["cost_unit"]
                profit_unit = item["profit_unit"]

                if prox is not None and 60.0 <= prox <= 92.0 and margin >= 20.0:
                    vol = get_14d_average_volume(int(tid)) if tid.isdigit() else -1.0
                    item["avg_daily_vol"] = vol

                    base_qty = max(1, int(round(1_500_000_000.0 / cost_unit))) if cost_unit > 0 else 1
                    if vol > 0:
                        vol_cap = max(1, int(round(vol * 0.35)))
                        final_qty = min(base_qty, vol_cap)
                    else:
                        final_qty = min(base_qty, max(1, int(item.get("qty", 1))))

                    item["qty"] = final_qty
                    item["capital"] = round(cost_unit * final_qty, 2)
                    item["profit"] = round(profit_unit * final_qty, 2)

                    if 0 <= vol < 2.5 and final_qty <= 1 and item["capital"] < 500_000_000.0:
                        item["action_code"] = "PATIENT"
                        item["action_text"] = "LOW VELOCITY"
                    else:
                        item["action_code"] = "PRIME_DIP"
                        item["action_text"] = "PRIME DIP"

                elif prox is not None and prox < 60.0:
                    item["action_code"] = "PATIENT"
                    item["action_text"] = "DEEP CRASH"
                elif prox is not None and prox > 92.0:
                    item["action_code"] = "PATIENT"
                    item["action_text"] = "NEAR PEAK"
                else:
                    item["action_code"] = "UNPLACED"
                    item["action_text"] = "NEW ORDER"
            else:
                item["action_code"] = "UNPLACED"
                item["action_text"] = "READY TO PLACE"

    # Hold Desk curation: active orders + Top 8 Prime Dips
    if desk_type == "hold":
        active_items = [it for it in items if it.get("char_status") != "UNPLACED"]
        unplaced_prime = [
            it for it in items
            if it.get("char_status") == "UNPLACED" and it.get("action_code") == "PRIME_DIP"
        ]
        unplaced_prime.sort(key=lambda x: -x.get("profit", 0.0))
        return active_items + unplaced_prime[:8]

    return items

_DESK_ANNOTATION_CACHE = None
_DESK_CACHE_TIME = 0.0


def _inject_orphaned_buys(eg: list, tac: list, hld: list, buys: dict, market_prices: dict):
    """Surfaces active buy orders whose items rotated out of all CSV ledgers so they can be cancelled."""
    known_tids = {str(r.get("type_id", "")) for r in (eg + tac + hld)}
    orphan_tids = [int(tid) for tid in buys.keys() if str(tid) not in known_tids and str(tid).isdigit()]
    if not orphan_tids:
        return

    global _ORPHAN_NAME_CACHE
    if "_ORPHAN_NAME_CACHE" not in globals():
        _ORPHAN_NAME_CACHE = {}

    desk_lookup = {}
    try:
        import duckdb
        con = duckdb.connect("data/eve_analytics.duckdb", read_only=True)
        id_str = ",".join(str(t) for t in orphan_tids)
        rows = con.execute(f"""
            SELECT type_id, desk_type, target_sell, name
            FROM (
                SELECT type_id, desk_type, target_sell, name,
                       ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY snapshot_time DESC) as rn
                FROM ledger_history WHERE type_id IN ({id_str})
            ) WHERE rn = 1
        """).fetchall()
        con.close()
        for r in rows:
            desk_lookup[r[0]] = (str(r[1]).lower(), float(r[2] or 0.0))
            if r[3] and not str(r[3]).startswith("Type "):
                _ORPHAN_NAME_CACHE[int(r[0])] = str(r[3])
    except Exception:
        pass

    missing = [t for t in orphan_tids if t not in _ORPHAN_NAME_CACHE]
    if missing:
        try:
            from services.market import resolve_type_ids
            for k, v in (resolve_type_ids(missing) or {}).items():
                if v and not str(v).startswith("Type "):
                    _ORPHAN_NAME_CACHE[int(k)] = str(v)
        except Exception:
            pass

    missing = [t for t in orphan_tids if t not in _ORPHAN_NAME_CACHE]
    if missing:
        try:
            import requests
            res = requests.post(
                "https://esi.evetech.net/latest/universe/names/?datasource=tranquility",
                json=missing,
                timeout=5
            )
            if res.status_code == 200:
                for entry in res.json():
                    _ORPHAN_NAME_CACHE[int(entry["id"])] = str(entry["name"])
        except Exception:
            pass

    name_map = _ORPHAN_NAME_CACHE

    buckets = {"evergreen": eg, "tactical": tac, "hold": hld}
    for tid_int in orphan_tids:
        tid_str = str(tid_int)
        b = buys[tid_str]
        desk_key, last_sell = desk_lookup.get(tid_int, ("evergreen", 0.0))
        target_list = buckets.get(desk_key, eg)
        p_info = market_prices.get(tid_str) or {}
        live_ask = p_info.get("lowest_ask")
        my_p = float(b.get("price", 0.0))
        qty = int(b.get("volume_remain", 1))
        escrow = float(b.get("escrow", my_p * qty))
        if live_ask and live_ask > 0:
            eff_sell = float(live_ask) * (0.985 if desk_key == "evergreen" else 1.0)
        else:
            eff_sell = last_sell
        fin = calc_line_financials(my_p, eff_sell, qty) if eff_sell > 0 else {
            "cost_unit": round(calc_unit_cost(my_p), 2),
            "net_proceeds_unit": 0.0,
            "profit_unit": 0.0,
            "capital": round(escrow, 2),
            "profit": 0.0,
            "net_margin": 0.0
        }
        prox_pct = round((float(live_ask) / last_sell) * 100.0, 1) if (live_ask and last_sell > 0) else None
        diff_pct = round(((float(live_ask) - last_sell) / last_sell) * 100.0, 1) if (live_ask and last_sell > 0) else 0.0
        from services.blacklist import load_active_blacklist
        bl_items, _, _ = load_active_blacklist()
        item_name = name_map.get(tid_int, f"Type {tid_int}")
        cancel_floor = 2.5 if desk_key == "evergreen" else (10.0 if desk_key == "hold" else 5.0)
        is_seasonal = any(k in item_name.lower() for k in ("tetrimon", "chemal tech", "keypass", "cipher", "shortcut", "gauntlet"))
        if is_seasonal:
            continue
        should_cancel = (item_name in bl_items) or eff_sell <= 0 or fin["net_margin"] < cancel_floor

        row = {
            "type_id": tid_int,
            "name": item_name,
            "qty": qty,
            "dump_freq": 0.0,
            "target_buy": round(my_p, 2),
            "target_sell": round(eff_sell, 2),
            "live_ask": p_info.get("lowest_ask"),
            "ask_polled": tid_str in market_prices,
            "proximity_pct": prox_pct,
            "diff_pct": diff_pct,
            "char_status": "BUY_ACTIVE",
            "my_price": my_p,
            "my_qty": qty,
            "escrow": round(escrow, 2),
            "price_synced": True,
            "action_code": "CANCEL_DROPPED" if should_cancel else "PATIENT",
            "action_text": "DROPPED & LOW MARGIN (CANCEL)" if should_cancel else f"ROTATED OUT (HOLD +{fin['net_margin']:.1f}%)"
        }
        row.update(fin)
        target_list.insert(0, row)

def get_annotated_desks():
    global _DESK_ANNOTATION_CACHE, _DESK_CACHE_TIME
    import time
    now = time.time()
    if _DESK_ANNOTATION_CACHE is not None and (now - _DESK_CACHE_TIME) < 5.0:
        return _DESK_ANNOTATION_CACHE
    evergreen = load_desk_csv("evergreen_ledger.csv")
    tactical = load_desk_csv("tactical_ledger.csv")
    hold = load_desk_csv("hold_ledger.csv")
    my_orders = load_cached_orders()
    market_prices = load_market_prices_cache()

    buys = my_orders.get("buys", {})
    sells = my_orders.get("sells", {})

    eg = annotate_desk_items(evergreen, "evergreen", market_prices, buys, sells)
    tac = annotate_desk_items(tactical, "tactical", market_prices, buys, sells)
    hld = annotate_desk_items(hold, "hold", market_prices, buys, sells)
    _inject_orphaned_buys(eg, tac, hld, buys, market_prices)

    _DESK_ANNOTATION_CACHE = (eg, tac, hld)
    _DESK_CACHE_TIME = now
    return _DESK_ANNOTATION_CACHE
