import time

from services.accounting import (
    calc_line_financials,
    calc_max_profitable_buy,
    calc_net_proceeds,
    calc_projected_margin,
    calc_unit_cost,
)
from services.desk import Desk
from services.desk_config import DeskProfile, LiveOrderPricing
from services.esi import get_14d_average_volume
from services.exclusions import load_active_blacklist
from services.ledgers import load_cached_orders, load_desk_csv
from services.storage import load_market_prices_cache
from services.strategy_versions import get_active_strategy_version, get_strategy_version

def resolve_target_prices(item: dict, desk_type: str, live_ask: float, live_top_buy: float, sells: dict) -> tuple:
    """Determines target buy, target sell, and recommended quantity per desk strategy."""
    csv_buy = float(item.get("target_buy", 0.0))
    csv_sell = float(item.get("target_sell", 0.0))
    tid = str(item.get("type_id", ""))
    qty = int(item.get("qty", 1))

    profile = Desk.get_desk_profile(desk_type)
    lcfg = getattr(profile, "live_pricing", None) or LiveOrderPricing()
    tick = lcfg.tick_increment

    if desk_type == "evergreen":
        if live_ask and live_ask > 0:
            spot_sell = live_ask - tick
            blended_sell = (
                (lcfg.spot_sell_weight * spot_sell) + (lcfg.csv_sell_weight * csv_sell)
                if (csv_sell > 0 and spot_sell < csv_sell)
                else spot_sell
            )
            t_sell = (
                min(blended_sell, csv_sell * lcfg.csv_sell_cap_mult)
                if (csv_sell > 0 and lcfg.csv_sell_cap_mult is not None)
                else spot_sell
            )
        else:
            t_sell = csv_sell
        max_buy = calc_max_profitable_buy(t_sell, min_margin_pct=profile.live_min_margin)
        t_buy = min(live_top_buy + tick, max_buy) if (live_top_buy and live_top_buy > 0) else min(csv_buy, max_buy)
        if lcfg.buy_ceil_ratio is not None:
            ceil_guard = (live_ask * lcfg.buy_ceil_ratio) if live_ask else (t_sell * lcfg.buy_ceil_ratio)
            t_buy = min(t_buy, ceil_guard)

    elif desk_type == "tactical":
        t_sell = (
            (lcfg.spot_sell_weight * (live_ask - tick)) + (lcfg.csv_sell_weight * csv_sell)
            if (live_ask and live_ask > 0)
            else csv_sell
        )
        max_buy = calc_max_profitable_buy(t_sell, min_margin_pct=profile.live_min_margin)
        t_buy = min(live_top_buy + tick, max_buy) if (live_top_buy and live_top_buy > 0) else min(csv_buy, max_buy)
        if lcfg.buy_ceil_ratio is not None:
            ceil_guard = (live_ask * lcfg.buy_ceil_ratio) if live_ask else (t_sell * lcfg.buy_ceil_ratio)
            t_buy = min(t_buy, ceil_guard)

    else:  # hold
        t_sell = (live_ask - tick) if (tid in sells and live_ask and live_ask > csv_sell) else csv_sell
        t_buy = csv_buy
        if live_top_buy and live_top_buy > 0:
            t_buy = min(live_top_buy + tick, t_buy)
        if live_ask and live_ask > 0 and lcfg.buy_ceil_ratio is not None:
            t_buy = min(t_buy, live_ask * lcfg.buy_ceil_ratio)
        cost_unit = calc_unit_cost(t_buy)
        if tid in sells:
            qty = int(item.get("qty", 1))
        else:
            qty = max(1, int(round(profile.max_line_cap / cost_unit))) if cost_unit > 0 else 1

    return round(t_buy, 2), round(t_sell, 2), qty


def evaluate_order_action(
    desk_type: str,
    my_price: float,
    top_buy: float,
    t_sell: float,
    dump_freq: float,
    proximity_pct: float = None,
    csv_sell: float = 0.0,
    live_ask: float = None,
) -> tuple:
    """Evaluates whether an active order is winning, viable to bump, or needs cancellation (with weekend-dip hysteresis)."""
    cfg = Desk.get_desk_profile(desk_type)
    min_margin = cfg.live_min_margin
    cancel_floor = cfg.cancel_floor
    if desk_type in ("evergreen", "tactical") and live_ask and float(live_ask) > 0:
        effective_sell = min(t_sell, float(live_ask))
    else:
        effective_sell = t_sell
    max_buy = calc_max_profitable_buy(effective_sell, min_margin)
    max_hold_buy = calc_max_profitable_buy(effective_sell, cancel_floor)
    cur_margin = calc_projected_margin(my_price, effective_sell)

    if max_hold_buy > 0 and my_price > (max_hold_buy * 1.005):
        return "CANCEL_OVERPRICED", f"OVERBID ({cur_margin:.1f}% LIVE MARGIN - CANCEL)"

    if desk_type == "hold" and proximity_pct is not None and proximity_pct > 92.0 and cur_margin < min_margin:
        return "CANCEL_RECOVERED", f"DIP RECOVERED ({cur_margin:.1f}% MARGIN - CANCEL)"

    if not top_buy or top_buy <= 0:
        return "PATIENT", "AWAITING QUOTE"

    if my_price >= (top_buy - 0.01):
        return "TOP_BOOK", "TOP BID (WINNING)"

    next_bid = top_buy + 0.01
    proj_margin = calc_projected_margin(next_bid, effective_sell)
    outbid_pct = ((top_buy - my_price) / my_price * 100.0) if my_price > 0 else 100.0

    if desk_type == "hold":
        in_active_dip = proximity_pct is not None and proximity_pct <= 82.0
        if in_active_dip and outbid_pct >= 1.5 and top_buy <= max_buy and proj_margin >= min_margin:
            return "BUMP_VIABLE", f"BUMP DIP (+{proj_margin:.1f}%)"
        return "PATIENT", "HOLD FIRM (WAIT DUMP)"

    min_outbid_pct = 1.0 if desk_type == "tactical" else 0.5
    if outbid_pct >= min_outbid_pct and top_buy <= max_buy and proj_margin >= min_margin:
        return "BUMP_VIABLE", f"BUMP VIABLE (+{proj_margin:.1f}%)"
    if desk_type in ("evergreen", "tactical"):
        my_margin = calc_projected_margin(my_price, effective_sell)
        if my_margin < cancel_floor:
            return "CANCEL_OVERPRICED", f"COMPRESSED ({my_margin:.1f}% MARGIN - CANCEL)"
        return "PATIENT", f"OUTBID (HOLD +{my_margin:.1f}%)"

    return "CANCEL_CEILING", "CEILING REACHED (CANCEL)"


def annotate_desk_items(items: list, desk_type: str, market_prices: dict, buys: dict, sells: dict) -> list:
    from services.strategy_versions import get_active_strategy_version, get_strategy_version
    profile = Desk.get_desk_profile(desk_type)
    lcfg = getattr(profile, "live_pricing", None) or LiveOrderPricing()

    for item in items:
        item["desk_type"] = desk_type
        tid = str(item.get("type_id", ""))
        p_info = market_prices.get(tid) or {}
        live_ask = p_info.get("lowest_ask")
        live_top_buy = p_info.get("top_buy")
        item["live_ask"] = live_ask
        item["ask_polled"] = tid in market_prices

        orig_csv_sell = float(item.get("target_sell", 0.0))
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
            item["price_synced"] = abs(b["price"] - t_buy) / t_buy < lcfg.price_sync_tolerance if t_buy > 0 else True
            dump_f = float(item.get("dump_freq", 0.0))
            active_ver = get_active_strategy_version()
            if active_ver != "v1":
                strat = get_strategy_version(active_ver)
                code, label = strat.evaluate_buy_order(
                    desk_type,
                    b["price"],
                    live_top_buy or 0.0,
                    t_sell,
                    dump_f,
                    item.get("proximity_pct"),
                    orig_csv_sell,
                    live_ask,
                    ask_days_of_supply=p_info.get("ask_days_of_supply"),
                    supply_class=str(item.get("supply_class") or "MANUFACTURED"),
                )
            else:
                code, label = evaluate_order_action(desk_type, b["price"], live_top_buy or 0.0, t_sell, dump_f, item.get("proximity_pct"), orig_csv_sell, live_ask)
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

                if (
                    prox is not None
                    and lcfg.hold_prime_prox_min <= prox <= lcfg.hold_prime_prox_max
                    and margin >= lcfg.hold_prime_min_margin
                ):
                    vol = get_14d_average_volume(int(tid)) if tid.isdigit() else -1.0
                    item["avg_daily_vol"] = vol

                    base_qty = max(1, int(round(profile.max_line_cap / cost_unit))) if cost_unit > 0 else 1
                    if vol > 0:
                        vol_cap = max(1, int(round(vol * profile.vol_pct)))
                        final_qty = min(base_qty, vol_cap)
                    else:
                        final_qty = min(base_qty, max(1, int(item.get("qty", 1))))

                    item["qty"] = final_qty
                    item["capital"] = round(cost_unit * final_qty, 2)
                    item["profit"] = round(profit_unit * final_qty, 2)

                    if (
                        0 <= vol < lcfg.hold_low_vol_threshold
                        and final_qty <= 1
                        and item["capital"] < lcfg.hold_low_vol_max_capital
                    ):
                        item["action_code"] = "PATIENT"
                        item["action_text"] = "LOW VELOCITY"
                    else:
                        item["action_code"] = "PRIME_DIP"
                        item["action_text"] = "PRIME DIP"

                elif prox is not None and prox < lcfg.hold_prime_prox_min:
                    item["action_code"] = "PATIENT"
                    item["action_text"] = "DEEP CRASH"
                elif prox is not None and prox > lcfg.hold_prime_prox_max:
                    item["action_code"] = "PATIENT"
                    item["action_text"] = "NEAR PEAK"
                else:
                    item["action_code"] = "UNPLACED"
                    item["action_text"] = "NEW ORDER"
            else:
                desk_cfg = Desk.get_desk_profile(desk_type)
                raw_allow = item.get("allow_new_buy", True)
                allow_new_buy = str(raw_allow).strip().lower() not in ("false", "0", "no")
                eff_live_sell = min(t_sell, float(live_ask)) if (live_ask and float(live_ask) > 0) else t_sell
                eff_live_margin = calc_projected_margin(t_buy, eff_live_sell) if t_buy > 0 else 0.0
                if not allow_new_buy or eff_live_margin < profile.live_min_margin:
                    item["action_code"] = "PATIENT"
                    item["action_text"] = f"DO NOT BUY ({eff_live_margin:.1f}% LIVE)"
                else:
                    item["action_code"] = "UNPLACED"
                    item["action_text"] = "READY TO PLACE"
    if desk_type == "hold":
        active_items = [it for it in items if it.get("char_status") != "UNPLACED"]
        unplaced_prime = [
            it for it in items
            if it.get("char_status") == "UNPLACED" and it.get("action_code") == "PRIME_DIP"
        ]
        unplaced_prime.sort(key=lambda x: -x.get("profit", 0.0))
        return active_items + unplaced_prime[: lcfg.hold_prime_dip_limit]

    return items


def _inject_orphaned_buys(eg: list, tac: list, hld: list, buys: dict, market_prices: dict):
    """Surfaces active buy orders whose items rotated out of all CSV ledgers so they can be cancelled."""
    known_tids = {str(r.get("type_id", "")) for r in (eg + tac + hld)}
    orphan_tids = [int(tid) for tid in buys.keys() if str(tid) not in known_tids and str(tid).isdigit()]
    if not orphan_tids:
        return

    global _ORPHAN_NAME_CACHE
    if "_ORPHAN_NAME_CACHE" not in globals():
        _ORPHAN_NAME_CACHE = {}

    from services.sde import get_latest_ledger_targets, resolve_type_ids
    desk_lookup = {}
    for tid_h, meta in get_latest_ledger_targets(orphan_tids).items():
        desk_lookup[tid_h] = (meta["desk"].lower(), meta["target_sell"])
        if meta["name"] and not meta["name"].startswith("Type "):
            _ORPHAN_NAME_CACHE[tid_h] = meta["name"]

    missing = [t for t in orphan_tids if t not in _ORPHAN_NAME_CACHE]
    if missing:
        for k, v in resolve_type_ids(missing).items():
            if v and not str(v).startswith("Type "):
                _ORPHAN_NAME_CACHE[int(k)] = str(v)

    missing = [t for t in orphan_tids if t not in _ORPHAN_NAME_CACHE]
    if missing:
        from services.esi import fetch_universe_names
        _ORPHAN_NAME_CACHE.update(fetch_universe_names(missing))

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
            spot_s = float(live_ask) * (0.985 if desk_key == "evergreen" else 1.0)
            eff_sell = ((0.55 * spot_s) + (0.45 * last_sell)) if (last_sell > 0 and spot_s < last_sell) else spot_s
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
        bl_items, _, _ = load_active_blacklist()
        item_name = name_map.get(tid_int, f"Type {tid_int}")
        cancel_floor = Desk.get_desk_profile(desk_key).cancel_floor
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

_DESK_ANNOTATION_CACHE = None
_DESK_CACHE_TIME = 0.0

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
