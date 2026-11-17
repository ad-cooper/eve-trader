from concurrent.futures import ThreadPoolExecutor
from services.ledgers import get_annotated_desks, get_desk_profile, load_cached_orders, load_market_prices_cache
from services.esi import fetch_jita_book_top
from services.blacklist import load_active_blacklist
from services.market import get_cost_basis_map
from services.accounting import (
    eve_tick,
    round_up_eve,
    round_down_eve,
    calc_projected_margin,
    calc_line_financials,
)


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

    if not live_ask or live_ask <= 0.01 or my_price <= (live_ask + 0.01):
        return "NONE", None

    next_ask = round_down_eve(float(live_ask) - eve_tick(float(live_ask)))
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
        update_ledger_market_prices()
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

            if action_code == "BUMP_VIABLE" and top_buy > 0:
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
