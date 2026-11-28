import os
import time
from services.accounting import (
    eve_tick,
    round_down_eve,
    calc_net_proceeds,
    calc_unit_cost,
)
from services.desk import Desk
from services.esi import (
    load_cached_assets,
    fetch_character_assets,
    fetch_character_orders,
    create_esi_session as create_session,
)
from constants import (
    JITA_STATION_ID,
    ORDERS_CACHE_FILE,
    TOTAL_EXIT_FEE as EXIT_FEE_JITA,
)
from services.sde import (
    parse_eve_item_lines as parse_multisell_export_text,
    resolve_type_names,
    resolve_type_ids,
    get_latest_ledger_targets as get_historical_ledger_targets,
)
from services.market import (
    ensure_jita_asks,
    get_cost_basis_map,
    get_active_sell_orders_map,
    get_hold_desk_params,
)
from services.strategy_versions import v2_evaluate_sell_collision


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
    active_qty = sum(o["volume_remain"] for o in active_orders)
    min_active_price = min((o["price"] for o in active_orders), default=0.0)
    total_stack_qty = active_qty + qty
    active_ratio = (active_qty / total_stack_qty) if total_stack_qty > 0 else 0.0
    tick = eve_tick(lowest_ask) if lowest_ask else 0.01
    is_undercut = bool(lowest_ask and lowest_ask < (min_active_price - tick))
    undercut_price = round_down_eve(lowest_ask - tick) if (lowest_ask and lowest_ask > tick) else min_active_price
    desk_upper = str(desk or "UNMANAGED").upper()

    collision_action = None
    collision_badge = None
    collision_reason = ""

    if desk_upper == "EVERGREEN":
        eg_min_roi = Desk.get_desk_profile("evergreen").take_profit_roi
        undercut_net = calc_net_proceeds(undercut_price) if lowest_ask else 0.0
        eff_cost = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
        still_profitable = (undercut_net >= eff_cost * (1.0 + eg_min_roi / 100.0)) if eff_cost > 0 else True
        if is_undercut and not still_profitable:
            action, action_type = "HOLD (MARGIN FLOOR)", "hold"
            collision_action, collision_badge = "HOLD_HANGAR", "🛡️ MARGIN FLOOR"
            collision_reason = f"Undercut {lowest_ask:,.2f} drops net margin below {eg_min_roi:.0f}%. Hold."
            rec_sell = round(min_active_price, 2)
        elif is_undercut:
            action, action_type = "UNDERCUT & FLIP", "flip"
            collision_action, collision_badge = "MATCH_ACTIVE", "⚡ UNDERCUT VIABLE"
            collision_reason = f"Undercutting {lowest_ask:,.2f} still clears >={eg_min_roi:.0f}% net margin. Rotate capital."
            rec_sell = undercut_price
        else:
            action, action_type = "MATCH ACTIVE ASK", "flip"
            collision_action, collision_badge = "TOP_OF_BOOK", "🟢 TOP OF BOOK"
            collision_reason = f"Active order ({active_qty}x @ {min_active_price:,.2f}) is top of book. Join stack."
            rec_sell = round(min_active_price, 2)

    elif desk_upper == "TACTICAL":
        tac_target_roi = Desk.get_desk_profile("tactical").min_margin
        undercut_net = calc_net_proceeds(undercut_price) if lowest_ask else 0.0
        eff_cost = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
        live_roi = ((undercut_net - eff_cost) / eff_cost * 100.0) if eff_cost > 0 else tac_target_roi
        if is_undercut:
            if active_ratio < 0.20 or (eff_cost > 0 and live_roi >= tac_target_roi):
                action, action_type = "CANCEL & MERGE", "flip"
                collision_action, collision_badge = "CANCEL_MERGE", "⚡ CANCEL & MERGE"
                collision_reason = f"Live ask clears +{live_roi:.1f}% net ROI (>={tac_target_roi:.1f}%). Merge full {total_stack_qty}x @ {undercut_price:,.2f}."
                rec_sell = undercut_price
            else:
                action, action_type = "MODIFY & HOLD", "hold"
                collision_action, collision_badge = "MODIFY_HOLD", "✏️ MODIFY & HOLD"
                collision_reason = f"Active stack {active_qty}x is {active_ratio*100:.1f}% (>=20%). Modify active order to {undercut_price:,.2f}; hold {qty}x reserve."
                rec_sell = round(min_active_price, 2)
        else:
            action, action_type = "FLIP (TACTICAL)", "flip"
            collision_action, collision_badge = "TOP_OF_BOOK", "🟢 TOP OF BOOK"
            collision_reason = f"Active order ({active_qty}x @ {min_active_price:,.2f}) is top of book. Join stack."
            rec_sell = round(min_active_price, 2)

    else:
        if is_undercut and hold_meta.get("is_expired"):
            action, action_type = f"TIME STOP ({hold_meta.get('days_held', 0)}d)", "time_stop"
            collision_action, collision_badge = "CANCEL_MERGE", "⏱️ TIME STOP"
            collision_reason = f"Position held {hold_meta.get('days_held', 0)}d (expired). Liquidate at {undercut_price:,.2f}."
            rec_sell = undercut_price
        elif is_undercut:
            action, action_type = "SELF-UNDERCUT RISK", "hold"
            collision_action, collision_badge = "HOLD_HANGAR", "⚠️ SELF-UNDERCUT"
            collision_reason = f"Active order exists @ {min_active_price:,.2f}. Avoid self-undercutting."
            rec_sell = round(min_active_price, 2)
        else:
            action, action_type = "MATCH ACTIVE ASK", "flip"
            collision_action, collision_badge = "TOP_OF_BOOK", "🟢 TOP OF BOOK"
            collision_reason = f"Active order ({active_qty}x @ {min_active_price:,.2f}) is top of book."
            rec_sell = round(min_active_price, 2)

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
    if desk == "EVERGREEN":
        eg_cfg = Desk.get_desk_profile("evergreen")
        days_held = hold_meta.get("days_held", 0)
        is_expired = hold_meta.get("is_expired", False) or (days_held >= eg_cfg.max_hold_days and days_held > 0)
        if lowest_ask and lowest_ask > 0.01:
            undercut_price = round(lowest_ask - 0.01, 2)
            eff_cost = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
            min_net = eff_cost * (1.0 + eg_cfg.take_profit_roi / 100.0)
            if eff_cost > 0 and not is_expired and calc_net_proceeds(undercut_price) < min_net:
                floor_sell = round(min_net / (1.0 - EXIT_FEE_JITA), 2)
                rec_sell = max(round(target_sell, 2) if target_sell else 0.0, floor_sell)
                return "HOLD (MARGIN FLOOR)", "hold", rec_sell
            if is_expired:
                return f"TIME STOP ({days_held}d)", "time_stop", undercut_price
            return "FAST FLIP", "flip", undercut_price
        rec_sell = round(target_sell, 2) if target_sell else 1000000.0
        return ("TIME STOP (0d)" if is_expired else "FAST FLIP"), ("time_stop" if is_expired else "flip"), rec_sell

    if desk in ["TACTICAL", "HOLD"]:
        desk_cfg = Desk.get_desk_profile(desk)
        days_held = hold_meta.get("days_held", 0)
        is_expired = hold_meta.get("is_expired", False)

        effective_cost_unit = calc_unit_cost(unit_cost) if unit_cost > 0 else 0.0
        net_proceeds_unit = calc_net_proceeds(lowest_ask - 0.01) if lowest_ask else 0.0
        live_roi_pct = ((net_proceeds_unit - effective_cost_unit) / effective_cost_unit * 100.0) if effective_cost_unit > 0 else 0.0
        min_target_roi = desk_cfg.take_profit_roi if desk_cfg.take_profit_roi > 0 else desk_cfg.sticky_margin_floor
        meets_profit_floor = (live_roi_pct >= min_target_roi) if effective_cost_unit > 0 else True
        late_cycle_days = max(desk_cfg.take_profit_days + 1, int(desk_cfg.max_hold_days * 0.65))
        late_cycle_lock = bool(days_held >= late_cycle_days and live_roi_pct >= 2.0)
        early_surge_lock = bool(desk == "TACTICAL" and days_held >= 2 and live_roi_pct >= 6.5)
        take_profit_ready = bool(
            lowest_ask
            and lowest_ask > 0.01
            and effective_cost_unit > 0
            and (
                (desk_cfg.take_profit_days > 0 and days_held >= desk_cfg.take_profit_days and live_roi_pct >= desk_cfg.take_profit_roi)
                or late_cycle_lock
                or early_surge_lock
            )
        )

        if lowest_ask and target_sell and lowest_ask >= target_sell:
            if meets_profit_floor:
                return "TARGET MET", "met", round(lowest_ask - 0.01, 2)
            floor_sell = round((effective_cost_unit * (1.0 + min_target_roi / 100.0)) / (1.0 - EXIT_FEE_JITA), 2)
            if is_expired:
                return f"LOW-MARGIN EXIT (+{live_roi_pct:.1f}%)", "time_stop", round(lowest_ask - 0.01, 2)
            return "HOLD (MARGIN COMPRESSED)", "hold", max(round(target_sell, 2), floor_sell)
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
        p_depth = (ctx.get("depth_map") or {}).get(tid) or {}
        s_class = (hist or {}).get("supply_class") or "MANUFACTURED"
        action, action_type, rec_sell, collision_info = v2_evaluate_sell_collision(
            desk,
            qty,
            unit_cost,
            lowest_ask,
            active_orders,
            hold_meta,
            ask_days_of_supply=p_depth.get("ask_days_of_supply"),
            supply_class=s_class,
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

    spot_sell = round_down_eve(lowest_ask - eve_tick(lowest_ask)) if (lowest_ask and lowest_ask > 0.01) else None
    spot_profit_unit = (calc_net_proceeds(spot_sell) - effective_cost_unit) if (spot_sell and effective_cost_unit > 0) else 0.0
    spot_profit = spot_profit_unit * qty
    spot_margin_pct = (spot_profit_unit / effective_cost_unit * 100.0) if (spot_sell and effective_cost_unit > 0) else 0.0

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
        "spot_profit": round(spot_profit, 2),
        "spot_margin_pct": round(spot_margin_pct, 1),
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


def evaluate_hangar_inventory(raw_text: str = None, force_refresh: bool = False):
    if force_refresh or (os.path.exists(ORDERS_CACHE_FILE) and (time.time() - os.path.getmtime(ORDERS_CACHE_FILE)) > 45):
        fetch_character_orders()
    targets = _collect_advisor_targets(raw_text=raw_text, force_refresh=force_refresh)
    if not targets:
        return _summarize_advisor_results([])

    session = create_session()
    type_ids = [t["type_id"] for t in targets]
    lowest_asks: dict[int, float | None] = {}
    ensure_jita_asks(session, type_ids, lowest_asks)

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