import os
import time
from concurrent.futures import ThreadPoolExecutor
from services.ledgers import (
    get_annotated_desks,
    get_desk_profile,
    load_cached_orders,
    load_market_prices_cache,
    ORDERS_CACHE_FILE,
)
from services.esi import (
    fetch_jita_book_top,
    fetch_character_orders,
    load_cached_assets,
    fetch_character_assets,
)
from services.blacklist import load_active_blacklist
from services.market import get_cost_basis_map, get_hold_desk_params
from services.sde import resolve_type_ids
from services.accounting import (
    eve_tick,
    round_up_eve,
    round_down_eve,
    calc_unit_cost,
    calc_net_proceeds,
    calc_projected_margin,
    calc_line_financials,
)


def _get_hangar_buy_cancel_reasons(
    buy_tids: list[int],
    rows_by_tid: dict[int, dict],
    market_prices: dict,
    buys_cache: dict | None = None,
) -> dict[int, str]:
    """
    Flags active BUY orders for cancellation when we already hold loose hangar stock of that
    item that is either time-stopped or underwater (net proceeds < unit cost) at live ask.
    """
    if not buy_tids:
        return {}
    assets = load_cached_assets(max_age_seconds=600) or fetch_character_assets() or []
    hangar_qtys: dict[int, int] = {}
    for a in assets:
        tid = a.get("type_id")
        if tid in buy_tids:
            hangar_qtys[int(tid)] = hangar_qtys.get(int(tid), 0) + int(a.get("quantity") or 1)

    reasons: dict[int, str] = {}
    buys_lookup = buys_cache or {}

    # 1. Order-slot worthiness: cancel residual partial-fill stubs below 85% of desk min_line_cap (e.g. <85M Tactical/EG, <127.5M Hold)
    for tid in buy_tids:
        row = rows_by_tid.get(tid) or {}
        b_info = buys_lookup.get(str(tid)) or {}
        desk_type = str(row.get("desk_type") or "evergreen").lower()
        cfg = get_desk_profile(desk_type)
        my_price = float(b_info.get("price") or row.get("my_price") or 0.0)
        vol_rem = int(b_info.get("volume_remain") or row.get("my_qty") or 0)
        vol_tot = int(b_info.get("volume_total") or vol_rem)
        rem_escrow = float(b_info.get("escrow") or row.get("escrow") or (my_price * vol_rem))
        is_partial = (vol_tot > vol_rem > 0) or (hangar_qtys.get(tid, 0) > 0 and vol_rem > 0)

        if is_partial and 0 < rem_escrow < (cfg.min_line_cap * 0.85):
            reasons[tid] = f"PARTIAL STUB ({rem_escrow / 1e6:.1f}M LEFT - CANCEL)"
        elif 0 < rem_escrow < (cfg.min_line_cap * 0.50):
            reasons[tid] = f"SMALL ORDER ({rem_escrow / 1e6:.1f}M - CANCEL)"

    overlapping_tids = [tid for tid, q in hangar_qtys.items() if q > 0 and tid not in reasons]
    if not overlapping_tids:
        return reasons

    name_map = resolve_type_ids(overlapping_tids)
    history_map = {
        tid: {
            "desk": str(rows_by_tid[tid].get("desk_type") or "EVERGREEN").upper(),
            "target_sell": float(rows_by_tid[tid].get("target_sell") or 0.0),
        }
        for tid in overlapping_tids
        if tid in rows_by_tid
    }
    cost_map = get_cost_basis_map(overlapping_tids, history_map)
    hold_map = get_hold_desk_params(overlapping_tids, name_map, history_map)

    for tid in overlapping_tids:
        row = rows_by_tid.get(tid) or {}
        desk_type = str(row.get("desk_type") or "evergreen").lower()
        cfg = get_desk_profile(desk_type)
        p_info = market_prices.get(str(tid)) or {}
        live_ask = float(p_info.get("lowest_ask") or 0.0)
        target_sell = float(row.get("target_sell") or 0.0)

        h_meta = hold_map.get(tid) or {}
        days_held = int(h_meta.get("days_held") or 0)
        is_expired = bool(h_meta.get("is_expired")) or (days_held >= cfg.max_hold_days and days_held > 0)

        unit_cost = float((cost_map.get(tid) or {}).get("unit_cost") or 0.0)
        eff_cost = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
        net_exit = calc_net_proceeds(live_ask) if live_ask > 0 else 0.0
        is_underwater = bool(eff_cost > 0 and live_ask > 0 and net_exit < eff_cost)

        if desk_type in ("tactical", "hold") and live_ask > 0 and target_sell > 0 and live_ask >= target_sell:
            reasons[tid] = "TARGET MET ON FILL (CANCEL REMAINDER)"
        elif is_expired and (live_ask <= 0 or target_sell <= 0 or live_ask < target_sell):
            reasons[tid] = f"HANGAR TIME STOP ({days_held}d - CANCEL)"
        elif is_underwater:
            loss_pct = ((net_exit - eff_cost) / eff_cost) * 100.0
            reasons[tid] = f"HANGAR AT LOSS ({loss_pct:.1f}% - CANCEL)"
    return reasons


def _evaluate_sell_order_action(
    item: dict,
    desk_type: str,
    sell_order: dict,
    live_ask: float | None,
    bl_items: set[str],
    real_unit_cost: float = 0.0,
) -> tuple[str, dict | None]:
    """
    Evaluates an active sell order on a ledger item to determine if it should be
    undercut (bumped down), cancelled (blacklisted), or held firm against real cost basis.
    """
    my_price = float(sell_order.get("price") or 0.0)
    qty = int(sell_order.get("volume_remain") or item.get("my_qty") or item.get("qty") or 1)
    if my_price <= 0 or qty <= 0:
        return "NONE", None

    name = str(item.get("name") or "")
    tid = int(item.get("type_id") or 0)
    desk_upper = desk_type.upper()
    ref_cost = real_unit_cost if real_unit_cost > 0 else float(item.get("target_buy") or 0.0)
    target_sell = float(item.get("target_sell") or 0.0)
    cfg = get_desk_profile(desk_type)

    if name in bl_items:
        return "CANCEL", {
            "side": "SELL",
            "type_id": tid,
            "name": name,
            "desk": desk_upper,
            "qty": qty,
            "current_price": round(my_price, 2),
            "market_price": round(float(live_ask), 2) if live_ask else None,
            "margin_pct": round(calc_projected_margin(ref_cost, my_price), 1) if ref_cost > 0 else 0.0,
            "escrow_freed": 0.0,
            "order_value": round(my_price * qty, 2),
            "action_code": "CANCEL_BLACKLIST",
            "reason": "BLACKLISTED ITEM (CANCEL)",
        }

    if not live_ask or live_ask <= 0.01:
        return "NONE", None

    tick = eve_tick(float(live_ask))
    if my_price <= (float(live_ask) + tick):
        return "NONE", None

    next_ask = round_down_eve(float(live_ask) - tick)
    if next_ask <= 0 or next_ask >= my_price:
        return "NONE", None

    min_floor = cfg.take_profit_roi if desk_type == "evergreen" else cfg.sticky_margin_floor
    new_margin = calc_projected_margin(ref_cost, next_ask) if ref_cost > 0 else min_floor
    price_ok = (new_margin >= min_floor) if ref_cost > 0 else (next_ask >= target_sell)

    if not price_ok:
        return "NONE", None

    fin = calc_line_financials(ref_cost, next_ask, qty=qty) if ref_cost > 0 else {"profit": 0.0}
    return "BUMP", {
        "side": "SELL",
        "type_id": tid,
        "name": name,
        "desk": desk_upper,
        "qty": qty,
        "unit_cost": round(ref_cost, 2),
        "current_price": round(my_price, 2),
        "market_price": round(float(live_ask), 2),
        "recommended_price": round(next_ask, 2),
        "price_delta": round(next_ask - my_price, 2),
        "escrow_delta": 0.0,
        "new_margin_pct": round(new_margin, 1),
        "expected_profit": fin["profit"],
        "action_code": "BUMP_SELL",
        "reason": f"UNDERCUT SELL (+{new_margin:.1f}%)" if ref_cost > 0 else "UNDERCUT TO TOP ASK",
    }


def get_order_actions(desk: str = "all", refresh_live: bool = False) -> dict:
    """
    Collects active SELL undercuts and BUY bid bumps separately from orders to cancel.
    """
    if refresh_live:
        from services.market import update_ledger_market_prices
        fetch_character_orders()
        update_ledger_market_prices()
        import services.ledgers as led_mod
        led_mod._DESK_ANNOTATION_CACHE = None
    elif os.path.exists(ORDERS_CACHE_FILE) and (time.time() - os.path.getmtime(ORDERS_CACHE_FILE)) > 45:
        fetch_character_orders()
        import services.ledgers as led_mod
        led_mod._DESK_ANNOTATION_CACHE = None

    eg, tac, hld = get_annotated_desks()
    desk_map = {
        "evergreen": eg,
        "tactical": tac,
        "hold": hld,
        "all": eg + tac + hld,
    }
    rows = desk_map.get(desk.lower(), eg + tac + hld)
    orders = load_cached_orders() or {}
    sells_cache = orders.get("sells") or {}
    market_prices = load_market_prices_cache() or {}
    bl_items, _, _ = load_active_blacklist()

    rows_by_tid = {int(r["type_id"]): r for r in rows if r.get("type_id")}
    active_buy_tids = [int(r["type_id"]) for r in rows if r.get("type_id") and r.get("char_status") == "BUY_ACTIVE"]
    hangar_cancel_reasons = _get_hangar_buy_cancel_reasons(active_buy_tids, rows_by_tid, market_prices, buys_cache=orders.get("buys") or {})

    sell_tids = [int(r["type_id"]) for r in rows if r.get("type_id") and r.get("char_status") == "SELL_ACTIVE"]
    cost_basis_map = get_cost_basis_map(sell_tids, {}) if sell_tids else {}

    live_books: dict[int, tuple[float, float]] = {}
    if refresh_live:
        active_tids = list({
            int(r["type_id"])
            for r in rows
            if r.get("type_id") and r.get("char_status") in ("BUY_ACTIVE", "SELL_ACTIVE")
        })
        if active_tids:
            with ThreadPoolExecutor(max_workers=12) as ex:
                for tid, top_b, low_s in ex.map(fetch_jita_book_top, active_tids):
                    live_books[tid] = (top_b, low_s)

    sell_bumps: list[dict] = []
    buy_bumps: list[dict] = []
    cancels: list[dict] = []

    for r in rows:
        tid = int(r.get("type_id") or 0)
        if not tid:
            continue
        tid_str = str(tid)
        desk_type = str(r.get("desk_type") or "evergreen").lower()
        desk_upper = desk_type.upper()
        status = r.get("char_status")
        action_code = str(r.get("action_code") or "")
        p_info = market_prices.get(tid_str) or {}
        top_buy, live_ask = live_books.get(tid, (p_info.get("top_buy") or 0.0, p_info.get("lowest_ask") or 0.0))

        if status == "BUY_ACTIVE":
            my_price = float(r.get("my_price") or 0.0)
            qty = int(r.get("my_qty") or r.get("qty") or 1)
            target_sell = float(r.get("target_sell") or 0.0)
            escrow = float(r.get("escrow") or (my_price * qty))

            hangar_reason = hangar_cancel_reasons.get(tid)
            if hangar_reason:
                cur_margin = round(calc_projected_margin(my_price, target_sell), 1) if target_sell > 0 else 0.0
                fin = calc_line_financials(my_price, target_sell, qty=qty) if target_sell > 0 else {"profit": 0.0}
                cancels.append({
                    "side": "BUY",
                    "type_id": tid,
                    "name": r.get("name"),
                    "desk": desk_upper,
                    "qty": qty,
                    "current_price": round(my_price, 2),
                    "market_price": round(float(top_buy), 2) if top_buy > 0 else (round(float(live_ask), 2) if live_ask > 0 else None),
                    "target_sell": round(target_sell, 2) if target_sell > 0 else None,
                    "expected_profit": fin["profit"],
                    "margin_pct": cur_margin,
                    "escrow_freed": round(escrow, 2),
                    "order_value": round(escrow, 2),
                    "action_code": "CANCEL_HANGAR_LOSS",
                    "reason": hangar_reason,
                })
            elif action_code == "BUMP_VIABLE" and top_buy > 0:
                rec_buy = round_up_eve(float(top_buy) + eve_tick(float(top_buy)))
                price_delta = round(rec_buy - my_price, 2)
                escrow_delta = round(max(0.0, price_delta) * qty, 2)
                new_margin = round(calc_projected_margin(rec_buy, target_sell), 1) if target_sell > 0 else 0.0
                fin = calc_line_financials(rec_buy, target_sell, qty=qty) if target_sell > 0 else {"profit": 0.0}
                buy_bumps.append({
                    "side": "BUY",
                    "type_id": tid,
                    "name": r.get("name"),
                    "desk": desk_upper,
                    "qty": qty,
                    "current_price": round(my_price, 2),
                    "market_price": round(float(top_buy), 2),
                    "recommended_price": round(rec_buy, 2),
                    "price_delta": price_delta,
                    "escrow_delta": escrow_delta,
                    "new_margin_pct": new_margin,
                    "expected_profit": fin["profit"],
                    "action_code": action_code,
                    "reason": r.get("action_text") or f"BUMP VIABLE (+{new_margin:.1f}%)",
                })
            elif action_code.startswith("CANCEL_"):
                cur_margin = round(calc_projected_margin(my_price, target_sell), 1) if target_sell > 0 else 0.0
                fin = calc_line_financials(my_price, target_sell, qty=qty) if target_sell > 0 else {"profit": 0.0}
                cancels.append({
                    "side": "BUY",
                    "type_id": tid,
                    "name": r.get("name"),
                    "desk": desk_upper,
                    "qty": qty,
                    "current_price": round(my_price, 2),
                    "market_price": round(float(top_buy), 2) if top_buy > 0 else (round(float(live_ask), 2) if live_ask > 0 else None),
                    "target_sell": round(target_sell, 2) if target_sell > 0 else None,
                    "expected_profit": fin["profit"],
                    "margin_pct": cur_margin,
                    "escrow_freed": round(escrow, 2),
                    "order_value": round(escrow, 2),
                    "action_code": action_code,
                    "reason": r.get("action_text") or "CANCEL ORDER",
                })

        elif status == "SELL_ACTIVE" and tid_str in sells_cache:
            real_cost = float((cost_basis_map.get(tid) or {}).get("unit_cost") or 0.0)
            kind, payload = _evaluate_sell_order_action(
                r, desk_type, sells_cache[tid_str], live_ask, bl_items, real_unit_cost=real_cost
            )
            if kind == "BUMP" and payload:
                sell_bumps.append(payload)
            elif kind == "CANCEL" and payload:
                cancels.append(payload)

    sell_bumps.sort(key=lambda x: (-x["expected_profit"], x["name"] or ""))
    buy_bumps.sort(key=lambda x: (-x["expected_profit"], x["name"] or ""))
    cancels.sort(key=lambda x: (-x["escrow_freed"], x["name"] or ""))

    all_bumps = sell_bumps + buy_bumps
    total_escrow_freed = round(sum(x["escrow_freed"] for x in cancels), 2)
    total_bump_escrow_delta = round(sum(x["escrow_delta"] for x in buy_bumps), 2)

    return {
        "total_actions": len(all_bumps) + len(cancels),
        "bump_count": len(all_bumps),
        "sell_bump_count": len(sell_bumps),
        "buy_bump_count": len(buy_bumps),
        "cancel_count": len(cancels),
        "total_escrow_freed": total_escrow_freed,
        "total_bump_escrow_delta": total_bump_escrow_delta,
        "net_isk_impact": round(total_escrow_freed - total_bump_escrow_delta, 2),
        "sell_bumps": sell_bumps,
        "buy_bumps": buy_bumps,
        "bumps": all_bumps,
        "cancels": cancels,
    }
