import math
import os
import pandas as pd

from services.accounting import (
    calc_line_financials,
    calc_net_proceeds,
    calc_projected_margin,
    calc_unit_cost,
)
from services.desk import Desk, IncumbentState, resolve_desk_incumbents
from services.desk_config import DESK_PROFILES, DeskProfile
from services.storage import (
    load_desk_csv,
    load_market_prices_cache, 
    load_cached_orders,
    read_csv_records, 
    read_json
)

def _resolve_desk_context(
    desk: Desk | str | None = None,
    desk_type: str | None = None,
    profile: DeskProfile | None = None,
    cfg: DeskProfile | None = None,
) -> tuple[str, DeskProfile]:
    if isinstance(desk, Desk):
        resolved_profile = cfg or profile or desk.profile
        resolved_type = desk.desk_type or (desk_type or "evergreen")
        return str(resolved_type).strip().lower(), resolved_profile

    raw_type = desk if isinstance(desk, str) else desk_type
    resolved_type = str(raw_type or "evergreen").strip().lower()
    resolved_profile = cfg or profile or Desk.get_desk_profile(resolved_type)
    return resolved_type, resolved_profile


def select_desk_candidates(
    df: pd.DataFrame,
    desk_or_type: Desk | str,
    base_mask: pd.Series,
    desk_sticky_ids: set[int],
    scale_factor: float = 1.0,
    profile: DeskProfile | None = None,
    cfg: DeskProfile | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Filters, scores, and ranks universe candidates for a desk using its Desk/DeskProfile."""
    desk_type, cfg = _resolve_desk_context(desk=desk_or_type, profile=profile, cfg=cfg)
    scfg = cfg.selection
    min_bid_share, max_bid_share = cfg.bid_share_bounds
    is_sticky_series = df["type_id"].isin(desk_sticky_ids)

    desk_mask = (
        df[cfg.price_col].between(cfg.min_price_m, cfg.max_price_m)
        & (df["daily_turnover_m"] >= cfg.min_turnover_m)
        & (df["avg_spread_30d"] >= cfg.min_spread_pct)
    )
    if cfg.excluded_categories:
        desk_mask &= ~df["category_name"].isin(cfg.excluded_categories)
    if cfg.min_history_days > 0:
        desk_mask &= df["total_history_days"] >= cfg.min_history_days
    if cfg.min_dump_freq_pct > 0:
        desk_mask &= df["dump_freq_pct"] >= cfg.min_dump_freq_pct
    if cfg.min_daily_vol > 0:
        desk_mask &= df["daily_vol_30d"] >= cfg.min_daily_vol
    if cfg.max_vwap_ratio_180d is not None:
        desk_mask &= df["price_m_30d"] <= (df["price_m_180d"] * cfg.max_vwap_ratio_180d)
    if "bid_share_30d" in df.columns and desk_type in ("tactical", "evergreen"):
        desk_mask &= df["bid_share_30d"].fillna(scfg.default_bid_share_30d) >= scfg.min_bid_share_30d

    candidates = df[base_mask & (desk_mask | is_sticky_series)].copy()
    candidates["passes_desk"] = desk_mask.loc[candidates.index]
    candidates["is_sticky"] = candidates["type_id"].isin(desk_sticky_ids)

    max_cap_m = (cfg.max_line_cap * scale_factor) / 1e6
    bid_share_series = (
        candidates["bid_share_30d"].fillna(scfg.default_bid_share_30d).clip(lower=min_bid_share, upper=max_bid_share)
        if "bid_share_30d" in candidates.columns
        else scfg.fallback_bid_share_30d
    )
    deployable_m = (candidates["daily_turnover_m"] * bid_share_series * cfg.vol_pct).clip(upper=max_cap_m)
    net_spread = (candidates["avg_spread_30d"] - scfg.edge_baseline_pct).clip(lower=0.0)
    log_weight_base = scfg.log_turnover_weight_base

    if desk_type == "hold":
        raw_upside = (
            ((candidates["price_m_180d"] * scfg.hold_target_vwap180_mult) - candidates["price_m_30d"])
            / candidates["price_m_30d"]
            * 100.0
        )
        mom_low, mom_high = scfg.momentum_guard_bounds
        momentum_guard = (candidates["price_m_7d"] / candidates["price_m_30d"]).clip(lower=mom_low, upper=mom_high)
        candidates["upside_pct"] = (
            raw_upside
            * momentum_guard
            * (log_weight_base + candidates["daily_turnover_m"].clip(lower=0.0).map(math.log1p))
        )
    elif desk_type == "tactical":
        candidates["swing_score"] = (
            candidates["dump_freq_pct"]
            * net_spread
            * (log_weight_base + deployable_m.clip(lower=0.0).map(math.log1p))
        )
    else:
        candidates["profit_velocity"] = deployable_m * net_spread

    candidates = candidates.sort_values(by=["is_sticky", cfg.sort_col], ascending=[False, False])

    params = {
        "name": cfg.name,
        "vol_pct": cfg.vol_pct,
        "max_line_cap": cfg.max_line_cap * scale_factor,
        "min_line_cap": cfg.min_line_cap,
        "min_margin_req": cfg.min_margin,
        "live_min_margin": cfg.live_min_margin,
        "sticky_margin_floor": cfg.sticky_margin_floor,
    }
    return candidates, params


def compute_candidate_pricing(
    row: pd.Series,
    desk_or_type: Desk | str,
    p_info: dict,
    incumbents: IncumbentState,
    is_sticky: bool,
) -> dict | None:
    """
    Computes target_buy, target_sell, unit economics, and net_margin for a single candidate.
    Returns None if the candidate fails live-book or desk pricing guards.
    """
    desk_type, profile = _resolve_desk_context(desk=desk_or_type)
    pcfg = getattr(profile, "pricing", None) or DESK_PROFILES["evergreen"].pricing

    tid = int(row["type_id"])
    has_buy = tid in incumbents.active_buys
    has_sell = tid in incumbents.active_sells
    held_qty = int(incumbents.asset_qtys.get(tid, 0))

    vwap_30d = row["price_m_30d"] * 1e6
    vwap_180d = row["price_m_180d"] * 1e6
    vwap_7d = row["price_m_7d"] * 1e6
    p20_low_30d = (row.get("p20_low_m_30d") or row["price_m_30d"] * pcfg.fallback_low_ratio) * 1e6
    dump_freq = float(row["dump_freq_pct"])

    live_ask = p_info.get("lowest_ask")
    live_top_buy = p_info.get("top_buy")

    if not has_buy and not has_sell and held_qty == 0 and p_info and (not live_ask or live_ask <= 0):
        return None

    p_current = live_ask if (live_ask and live_ask > 0) else (vwap_7d if vwap_7d > 0 else vwap_30d)
    drift = abs(p_current - vwap_30d) / vwap_30d if vwap_30d > 0 else 0.0
    stability_score = max(
        pcfg.stability_min,
        min(
            pcfg.stability_max,
            1.0 - (pcfg.stability_drift_weight * drift) - (pcfg.stability_dump_weight * (dump_freq / 100.0)),
        ),
    )

    if has_buy:
        competitive_bid = float(incumbents.active_buys[tid]["price"])
    elif live_top_buy and live_top_buy > 0:
        competitive_bid = live_top_buy + pcfg.tick_increment
    else:
        competitive_bid = min(p20_low_30d, p_current * pcfg.fallback_bid_ratio)

    if desk_type == "hold":
        if (
            not is_sticky
            and live_ask
            and pcfg.max_live_ask_vwap180_ratio is not None
            and live_ask >= (vwap_180d * pcfg.max_live_ask_vwap180_ratio)
        ):
            return None
        t_sell = min(vwap_180d * pcfg.sell_vwap180_mult, vwap_30d * pcfg.sell_vwap30_mult)
        t_buy = competitive_bid
        if not has_buy and live_ask and live_ask > 0:
            t_buy = min(t_buy, live_ask * pcfg.buy_cap_ratio)

    elif desk_type == "tactical":
        stabilized_ask = p_current * (
            pcfg.stabilized_ask_base_weight + pcfg.stabilized_ask_score_weight * stability_score
        )
        baseline_sell = vwap_30d * pcfg.sell_vwap30_mult
        if live_ask and float(live_ask) > 0:
            baseline_sell = min(baseline_sell, float(live_ask) * pcfg.sell_ask_cap_mult)
        t_sell = (pcfg.sell_stabilized_weight * stabilized_ask) + (pcfg.sell_baseline_weight * baseline_sell)
        t_buy = competitive_bid
        if not has_buy and live_ask and live_ask > 0:
            t_buy = min(t_buy, live_ask * pcfg.buy_cap_ratio)

    else:  # evergreen
        stabilized_ask = p_current * (
            pcfg.stabilized_ask_base_weight + pcfg.stabilized_ask_score_weight * stability_score
        )
        baseline_sell = vwap_30d * pcfg.sell_vwap30_mult
        if live_ask and float(live_ask) > 0:
            baseline_sell = min(baseline_sell, float(live_ask) * pcfg.sell_ask_cap_mult)
        blended_sell = (pcfg.sell_stabilized_weight * stabilized_ask) + (pcfg.sell_baseline_weight * baseline_sell)
        t_sell = min(blended_sell, vwap_30d * pcfg.sell_vwap30_cap_mult)
        t_buy = competitive_bid
        if not has_buy:
            ceil_guard = (live_ask * pcfg.buy_cap_ratio) if live_ask else (t_sell * pcfg.buy_cap_ratio)
            t_buy = min(t_buy, ceil_guard)

    if has_sell and float(incumbents.active_sells[tid].get("price", 0.0)) > 0:
        t_sell = max(t_sell, float(incumbents.active_sells[tid]["price"]))

    cost_unit = calc_unit_cost(t_buy)
    proceeds_unit = calc_net_proceeds(t_sell)
    net_profit_unit = proceeds_unit - cost_unit
    net_margin = calc_projected_margin(t_buy, t_sell)
    live_exec_sell = min(t_sell, float(live_ask)) if (desk_type != "hold" and live_ask and float(live_ask) > 0) else t_sell
    live_exec_margin = calc_projected_margin(t_buy, live_exec_sell)

    return {
        "target_buy": t_buy,
        "target_sell": t_sell,
        "cost_unit": cost_unit,
        "net_profit_unit": net_profit_unit,
        "net_margin": net_margin,
        "live_exec_margin": live_exec_margin,
        "dump_freq": dump_freq,
    }


def compute_line_sizing(
    row: pd.Series,
    pricing: dict,
    desk_params: dict,
    incumbents: IncumbentState,
    is_sticky: bool,
    passes_desk: bool,
    remaining_budget: float,
    desk: Desk | None = None,
) -> dict | None:
    """
    Applies margin floors, per-line ISK caps, sticky quantity preservation, and budget clamping.
    Returns None if the candidate should be skipped.
    """
    desk_name, profile = _resolve_desk_context(desk=desk, desk_type=desk_params.get("name"))
    min_bid_share, max_bid_share = profile.bid_share_bounds

    tid = int(row["type_id"])
    has_buy = tid in incumbents.active_buys
    has_sell = tid in incumbents.active_sells
    held_qty = int(incumbents.asset_qtys.get(tid, 0))

    cost_unit = pricing["cost_unit"]
    net_profit_unit = pricing["net_profit_unit"]
    net_margin = pricing["net_margin"]
    live_exec_margin = float(pricing.get("live_exec_margin", net_margin))
    live_min_margin = float(desk_params.get("live_min_margin", desk_params["min_margin_req"]))

    req_margin = desk_params["sticky_margin_floor"] if is_sticky else desk_params["min_margin_req"]
    if not has_sell and held_qty == 0 and live_exec_margin < req_margin:
        return None

    max_line_cap = desk_params["max_line_cap"]
    min_line_cap = desk_params["min_line_cap"]
    vol_pct = desk_params["vol_pct"]

    if not is_sticky and cost_unit > max_line_cap:
        return None

    raw_bid_share = row.get("bid_share_30d")
    if raw_bid_share is not None and not (isinstance(raw_bid_share, float) and math.isnan(raw_bid_share)):
        bid_share = max(min_bid_share, min(max_bid_share, float(raw_bid_share)))
    else:
        bid_share = 1.0

    effective_bid_vol = row["daily_vol_30d"] * (bid_share if desk_name != "hold" else 1.0)
    raw_qty = max(1, int(effective_bid_vol * vol_pct))
    cap_qty = max(1, int(max_line_cap / cost_unit)) if cost_unit > 0 else 1
    qty = min(raw_qty, cap_qty)

    buy_rem = int(incumbents.active_buys[tid].get("volume_remain", 0)) if has_buy else 0
    sell_rem = int(incumbents.active_sells[tid].get("volume_remain", 0)) if has_sell else 0
    existing_qty = max(buy_rem, sell_rem + held_qty)

    allow_new_buy = passes_desk and (live_exec_margin >= live_min_margin)
    if is_sticky:
        if has_sell or held_qty > 0:
            qty = max(qty, existing_qty) if allow_new_buy else max(1, existing_qty)
        else:
            if live_exec_margin < desk_params["sticky_margin_floor"]:
                return None
            qty = max(1, existing_qty)

    capital_line = cost_unit * qty

    effective_min_cap = (min_line_cap * profile.sticky_min_cap_ratio) if is_sticky else min_line_cap
    if not has_buy and capital_line < effective_min_cap:
        return None

    if capital_line > remaining_budget:
        if is_sticky and existing_qty > 0:
            qty = existing_qty
        else:
            qty = int(remaining_budget / cost_unit)
            if qty < 1 or (cost_unit * qty) < effective_min_cap:
                return None
        capital_line = cost_unit * qty

    return {
        "qty": qty,
        "allow_new_buy": allow_new_buy,
        "capital": capital_line,
        "profit": net_profit_unit * qty,
    }


def get_desk_claimed_type_ids(include_active_orders: bool = True) -> set[int]:
    """Returns all type_ids currently managed by Jita desks (CSVs + active Jita orders)."""
    tids: set[int] = set()
    for fname in ("evergreen_ledger.csv", "tactical_ledger.csv", "hold_ledger.csv"):
        for row in load_desk_csv(fname):
            if row.get("type_id"):
                try:
                    tids.add(int(row["type_id"]))
                except (ValueError, TypeError):
                    pass
    if include_active_orders:
        orders = load_cached_orders() or {}
        for bucket in ("buys", "sells"):
            for k in (orders.get(bucket) or {}).keys():
                if str(k).isdigit():
                    tids.add(int(k))
    return tids

get_desk_profile = Desk.get_desk_profile