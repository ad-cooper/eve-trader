from dataclasses import dataclass
import os
import math
import pandas as pd
from services.exclusions import build_static_exclusion_mask, is_item_excluded, load_active_blacklist
from services.storage import read_json, read_csv_records, load_market_prices_cache
from services.esi import get_14d_average_volume
from services.desk_config import (
    DeskPricingConfig,
    DeskProfile,
    DESK_PROFILES,
    LiveOrderPricing as lcfg

)
from services.accounting import (
    calc_unit_cost,
    calc_net_proceeds,
    calc_max_profitable_buy,
    calc_projected_margin,
    calc_line_financials,
)

from constants import (
    LEDGER_DIR,
    MARKET_CACHE_FILE,
    ORDERS_CACHE_FILE
)

def get_desk_profile(desk_type: str | None) -> DeskProfile:
    """Case-insensitive lookup for a DeskProfile, defaulting to 'evergreen'."""
    key = str(desk_type or "evergreen").strip().lower()
    if key not in DESK_PROFILES:
        return DESK_PROFILES["evergreen"]
    return DESK_PROFILES[key]

def select_desk_candidates(
    df: pd.DataFrame,
    desk_type: str,
    base_mask: pd.Series,
    desk_sticky_ids: set[int],
    scale_factor: float = 1.0,
    profile: DeskProfile | None = None,
    cfg: DeskProfile | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Filters, scores, and ranks universe candidates for a desk using its DeskProfile."""
    cfg = cfg or (profile or DESK_PROFILES[desk_type])
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

@dataclass(frozen=True)
class IncumbentState:
    active_buys: dict[int, dict]
    active_sells: dict[int, dict]
    asset_qtys: dict[int, int]
    significant_assets: set[int]
    desk_sticky_ids: set[int]


def resolve_desk_incumbents(
    df: pd.DataFrame,
    desk_type: str,
    claimed_ids: set[int],
    active_buys: dict[int, dict],
    active_sells: dict[int, dict],
    asset_qtys: dict[int, int],
    prior_desks: dict[int, str],
    min_asset_value_isk: float = 50_000_000.0,
) -> IncumbentState:
    """Identifies active orders and high-value hangar stacks that must remain sticky on this desk."""
    price_lookup = dict(zip(df["type_id"].astype(int), df["price_m_30d"] * 1e6))
    significant_assets = {
        tid for tid, qty in asset_qtys.items()
        if (qty * price_lookup.get(tid, 0.0)) >= min_asset_value_isk
    }
    incumbent_ids = set(active_buys.keys()) | set(active_sells.keys()) | significant_assets

    other_prior_desks = {tid for tid, d in prior_desks.items() if d != desk_type}
    if desk_type == "evergreen":
        desk_sticky_ids = (incumbent_ids - claimed_ids) - other_prior_desks
    else:
        desk_sticky_ids = {
            tid for tid in (incumbent_ids - claimed_ids)
            if prior_desks.get(tid) == desk_type
        }

    return IncumbentState(
        active_buys=active_buys,
        active_sells=active_sells,
        asset_qtys=asset_qtys,
        significant_assets=significant_assets,
        desk_sticky_ids=desk_sticky_ids,
    )


def build_universe_exclusion_mask(
    df: pd.DataFrame,
    claimed_ids: set[int],
    incumbents: IncumbentState,
    bl_items: set[str] | None = None,
    bl_groups: set[str] | None = None,
    bl_meta: dict | None = None,
    losing_tids: set[int] | None = None,
    extra_excluded_categories: tuple[str, ...] = (),
) -> pd.Series:
    """Builds the boolean eligibility mask separating permanent/seasonal exclusions from desk screening."""
    keep = build_static_exclusion_mask(
        df=df,
        bl_items=bl_items,
        bl_groups=bl_groups,
        bl_meta=bl_meta,
        extra_excluded_categories=extra_excluded_categories,
    )

    sticky_ids = set(incumbents.desk_sticky_ids or ()) if incumbents else set()
    tids = df["type_id"] if "type_id" in df.columns else None

    if claimed_ids and tids is not None:
        keep &= ~tids.isin(claimed_ids)

    if losing_tids and tids is not None:
        protected = (
            set((incumbents.active_sells or {}).keys())
            | set(incumbents.significant_assets or ())
            | sticky_ids
        )
        blocked_losers = set(losing_tids) - protected
        if blocked_losers:
            keep &= ~tids.isin(blocked_losers)

    if "price_m_7d" in df.columns and "price_m_30d" in df.columns:
        dip_cfg = DESK_PROFILES["evergreen"].selection
        valid_30d = df["price_m_30d"] > 0
        valid_7d = df["price_m_7d"] > 0
        ratio = df["price_m_7d"] / df["price_m_30d"].replace(0, float("nan"))
        dip_mask = (valid_30d & valid_7d & (ratio < dip_cfg.dip_ratio_7d_30d)) | (
            valid_30d & (~valid_7d | (ratio < dip_cfg.sticky_dip_ratio_7d_30d))
        )
        if tids is not None and sticky_ids:
            dip_mask &= ~tids.isin(sticky_ids)
        keep &= ~dip_mask

    return keep

def compute_candidate_pricing(
    row: pd.Series,
    desk_type: str,
    p_info: dict,
    incumbents: IncumbentState,
    is_sticky: bool,
) -> dict | None:
    """
    Computes target_buy, target_sell, unit economics, and net_margin for a single candidate.
    Returns None if the candidate fails live-book or desk pricing guards.
    """
    profile = DESK_PROFILES.get(desk_type, DESK_PROFILES["evergreen"])
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

    # Skip unplaced items whose live telemetry has been polled and shows zero active sell orders
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
) -> dict | None:
    """
    Applies margin floors, per-line ISK caps, sticky quantity preservation, and budget clamping.
    Returns None if the candidate should be skipped.
    """
    desk_name = str(desk_params.get("name") or "")
    profile = DESK_PROFILES.get(desk_name, DESK_PROFILES["evergreen"])
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
            # Preserve existing_qty so sell/hangar inventory stays tracked on ledger; block expansion if spread compressed
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


def is_item_blacklisted(
    name: str,
    group_name: str,
    bl_items: set,
    bl_groups: set,
    bl_meta: dict,
) -> bool:
    """Checks exact item/group exclusions plus active regex patterns from temporary_blacklist.json."""
    return is_item_excluded(
        name=name,
        group_name=group_name,
        category_name="",
        bl_items=bl_items,
        bl_groups=bl_groups,
        bl_meta=bl_meta,
        check_default_categories=False,
        check_default_name_regex=False,
    )


def load_desk_csv(filename: str) -> list[dict]:
    return read_csv_records(os.path.join(LEDGER_DIR, filename))


def load_all_desk_ledgers() -> dict[str, list[dict]]:
    return {
        "evergreen": load_desk_csv("evergreen_ledger.csv"),
        "tactical": load_desk_csv("tactical_ledger.csv"),
        "hold": load_desk_csv("hold_ledger.csv"),
    }


def load_cached_orders() -> dict:
    return read_json(ORDERS_CACHE_FILE, default={"buys": {}, "sells": {}, "count": 0, "total_escrow": 0.0})


def load_sniper_deals() -> list:
    data = read_json("ledgers/sniper_deals.json", default=[])
    return data if isinstance(data, list) else data.get("deals", [])

def resolve_target_prices(item: dict, desk_type: str, live_ask: float, live_top_buy: float, sells: dict) -> tuple:
    """Determines target buy, target sell, and recommended quantity per desk strategy."""
    csv_buy = float(item.get("target_buy", 0.0))
    csv_sell = float(item.get("target_sell", 0.0))
    tid = str(item.get("type_id", ""))
    qty = int(item.get("qty", 1))

    profile = get_desk_profile(desk_type)
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
    cfg = get_desk_profile(desk_type)
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
    profile = get_desk_profile(desk_type)
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
            from services.strategy_versions import get_active_strategy_version, get_strategy_version
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
                desk_cfg = get_desk_profile(desk_type)
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
        cancel_floor = get_desk_profile(desk_key).cancel_floor
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


