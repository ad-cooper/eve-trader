from __future__ import annotations
import math
import os
import sqlite3
import pandas as pd

from services.accounting import (
    eve_tick,
    calc_max_profitable_buy,
    calc_projected_margin,
)
from services.desk import (
    Desk,
    IncumbentState,
)
from services.ledgers import (
    compute_candidate_pricing as _base_compute_candidate_pricing,
    compute_line_sizing as v2_compute_line_sizing,
)


# ==============================================================================
# Fundamental Microstructure Metrics (SDE Supply Elasticity + Book Depth)
# ==============================================================================

def compute_book_depth_stats(
    sell_orders: list[tuple[float, int]],
    daily_vol_30d: float = 10.0,
    band_pct: float = 0.025,
    clearing_vol_fraction: float = 0.40,
) -> dict:
    """
    Computes depth-aware ask statistics from sorted Jita 4-4 sell orders [(price, volume_remain), ...].
    Distinguishes 1-2 unit tourist lowballs from genuine supply saturation walls.
    """
    clean_asks = sorted(
        [(float(p), int(v)) for p, v in sell_orders if float(p) > 0 and int(v) > 0],
        key=lambda x: x[0],
    )
    if not clean_asks:
        return {
            "lowest_ask": 0.0,
            "front_queue_qty": 0,
            "ask_days_of_supply": 0.0,
            "effective_clearing_ask": 0.0,
            "is_thin_lowball": False,
        }

    raw_lowest = clean_asks[0][0]
    band_ceiling = raw_lowest * (1.0 + band_pct)
    front_queue_qty = sum(v for p, v in clean_asks if p <= band_ceiling)

    eff_daily_vol = max(float(daily_vol_30d or 0.0), 0.25)
    ask_days_of_supply = round(front_queue_qty / eff_daily_vol, 3)

    target_depth_qty = max(2.0, eff_daily_vol * clearing_vol_fraction)
    cum_qty = 0
    effective_clearing_ask = clean_asks[-1][0]
    for p, v in clean_asks:
        cum_qty += v
        if cum_qty >= target_depth_qty:
            effective_clearing_ask = p
            break

    spread_gap_pct = (
        ((effective_clearing_ask - raw_lowest) / effective_clearing_ask)
        if effective_clearing_ask > 0
        else 0.0
    )
    is_thin_lowball = bool(ask_days_of_supply < 0.35 and spread_gap_pct >= 0.03)

    return {
        "lowest_ask": round(raw_lowest, 2),
        "front_queue_qty": int(front_queue_qty),
        "ask_days_of_supply": ask_days_of_supply,
        "effective_clearing_ask": round(effective_clearing_ask, 2),
        "is_thin_lowball": is_thin_lowball,
    }


def enrich_universe_fundamentals(
    df: pd.DataFrame,
    sqlite_path: str = "data/latest-sqlite.db",
    duckdb_path: str = "data/eve_analytics.duckdb",
) -> pd.DataFrame:
    """
    Attaches fundamental supply-elasticity features to the universe DataFrame:
      - supply_class: 'MANUFACTURED' (fast industrial restock), 'LP_FACTION' (lumpy LP conversion),
        or 'FARMED_RNG' (inelastic deadspace/abyssal/exploration drops)
      - natural_cycle_days: intrinsic restock half-life in days (3d / 8d / 21d)
      - supply_elasticity: normalized [0.25 .. 0.90] score measuring how fast supply responds to shortages
    """
    out = df.copy()
    if out.empty:
        out["supply_class"] = pd.Series(dtype="object")
        out["natural_cycle_days"] = pd.Series(dtype="float64")
        out["supply_elasticity"] = pd.Series(dtype="float64")
        return out

    mfg_ids: set[int] = set()
    meta_map: dict[int, int] = {}

    if sqlite_path and os.path.exists(sqlite_path):
        try:
            con = sqlite3.connect(sqlite_path)
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "industryActivityProducts" in tables:
                mfg_ids = {
                    int(r[0])
                    for r in con.execute(
                        "SELECT DISTINCT productTypeID FROM industryActivityProducts WHERE activityID IN (1, 11)"
                    ).fetchall()
                    if r[0] is not None
                }
            if "invMetaTypes" in tables:
                meta_map = {
                    int(r[0]): int(r[1])
                    for r in con.execute(
                        "SELECT typeID, metaGroupID FROM invMetaTypes WHERE metaGroupID IS NOT NULL"
                    ).fetchall()
                    if r[0] is not None
                }
            con.close()
        except Exception:
            pass

    supply_classes: list[str] = []
    cycle_days_list: list[float] = []
    elasticity_list: list[float] = []

    for _, row in out.iterrows():
        tid = int(row["type_id"])
        grp = str(row.get("group_name") or "").lower()
        cat = str(row.get("category_name") or "").lower()
        name = str(row.get("item_name") or "").lower()
        mg = meta_map.get(tid)

        is_farmed_rng = (
            mg in (3, 5, 6)
            or any(k in grp for k in ("mutaplasmid", "decryptor", "ancient", "sleeper", "talocan", "abyssal"))
            or any(
                k in name
                for k in (
                    "mutaplasmid",
                    "pith ",
                    "pithum ",
                    "gist ",
                    "gistum ",
                    "gisti ",
                    "core ",
                    "corelum ",
                    "coreli ",
                    "corpus ",
                    "corpum ",
                    "corpii ",
                    "centum ",
                    "centus ",
                    "centii ",
                )
            )
        )
        is_lp_faction = (
            mg == 4
            or ("implant" in cat)
            or any(k in grp for k in ("cyberimplant", "booster"))
            or any(
                k in name
                for k in (
                    "imperial navy",
                    "caldari navy",
                    "federation navy",
                    "republic fleet",
                    "dread guristas",
                    "true sansha",
                    "dark blood",
                    "shadow serpentis",
                    "domination",
                    "syndicate",
                    "sisters ",
                )
            )
        )

        if is_farmed_rng:
            s_class, c_days, elast = "FARMED_RNG", 21.0, 0.25
        elif is_lp_faction:
            s_class, c_days, elast = "LP_FACTION", 8.0, 0.55
        elif tid in mfg_ids or mg in (1, 2) or cat in ("ship", "module", "charge", "drone", "rig", "structure", "subsystem"):
            s_class, c_days, elast = "MANUFACTURED", 3.0, 0.90
        else:
            s_class, c_days, elast = "FARMED_RNG", 14.0, 0.30

        supply_classes.append(s_class)
        cycle_days_list.append(c_days)
        elasticity_list.append(elast)

    out["supply_class"] = supply_classes
    out["natural_cycle_days"] = cycle_days_list
    out["supply_elasticity"] = elasticity_list
    return out


# ==============================================================================
# V2 Engine (Supply-Elasticity + Book-Depth Fundamental Engine)
# ==============================================================================

def v2_select_desk_candidates(
    df: pd.DataFrame,
    desk_type: str,
    base_mask: pd.Series,
    desk_sticky_ids: set[int],
    scale_factor: float = 1.0,
) -> tuple[pd.DataFrame, dict]:
    """
    V2 candidate selection driven by fundamental supply elasticity & natural cycle length:
      - Evergreen favors fast-restocking MANUFACTURED items with cap-filling net velocity.
      - Tactical favors lumpy LP_FACTION & high-dump FARMED_RNG swings (5-10d cycle).
      - Hold favors inelastic FARMED_RNG & LP_FACTION cyclical troughs (10-21d cycle).
    """
    cfg = Desk.get_desk_profile(desk_type)
    scfg = cfg.selection
    if "supply_class" not in df.columns:
        df = enrich_universe_fundamentals(df)

    is_sticky_series = df["type_id"].isin(desk_sticky_ids)
    s_class = df["supply_class"]

    p20_safe = df["p20_low_m_30d"].fillna(df["price_m_30d"]).replace(0, float("nan"))
    swing_amp_30d = (((df["price_m_30d"] - p20_safe) / p20_safe) * 100.0).fillna(0.0)
    spread_or_swing = (df["avg_spread_30d"] >= cfg.min_spread_pct) | (
        (desk_type in ("tactical", "hold")) & (swing_amp_30d >= scfg.swing_amp_min_pct)
    )
    desk_mask = (
        df[cfg.price_col].between(cfg.min_price_m, cfg.max_price_m)
        & (df["daily_turnover_m"] >= cfg.min_turnover_m)
        & spread_or_swing
    )
    if cfg.excluded_categories:
        desk_mask &= ~df["category_name"].isin(cfg.excluded_categories)
    if cfg.min_history_days > 0:
        desk_mask &= df["total_history_days"] >= cfg.min_history_days

    active_days = (
        df["active_days_30d"].fillna(30)
        if "active_days_30d" in df.columns
        else pd.Series(30, index=df.index)
    )
    if desk_type == "evergreen":
        desk_mask &= (
            (df["daily_vol_30d"] >= cfg.min_daily_vol)
            & (active_days >= 20)
            & ((s_class == "MANUFACTURED") | (df["daily_turnover_m"] >= scfg.turnover_archetype_floor_m))
        )
    elif desk_type == "tactical":
        desk_mask &= (
            (df["daily_vol_30d"] >= scfg.tactical_min_daily_vol)
            & (active_days >= 15)
            & (df["dump_freq_pct"] >= max(scfg.min_dump_freq_floor_pct, cfg.min_dump_freq_pct - scfg.dump_freq_floor_offset_pct))
            & (s_class.isin(["LP_FACTION", "FARMED_RNG"]) | (df["dump_freq_pct"] >= scfg.non_archetype_min_dump_freq_pct))
        )
    else:  # hold
        ratio_cap = cfg.max_vwap_ratio_180d if cfg.max_vwap_ratio_180d is not None else 0.90
        inelastic_hold = (df["price_m_30d"] <= (df["price_m_180d"] * ratio_cap)) & s_class.isin(
            ["FARMED_RNG", "LP_FACTION"]
        )
        deep_mfg_hold = (
            df["price_m_30d"]
            <= (df["price_m_180d"] * min(scfg.hold_deep_mfg_ratio_cap, ratio_cap - scfg.hold_deep_mfg_ratio_offset))
        ) & (s_class == "MANUFACTURED")
        desk_mask &= (
            (df["daily_vol_30d"] >= scfg.hold_min_daily_vol)
            & (active_days >= scfg.hold_min_active_days)
            & (inelastic_hold | deep_mfg_hold)
        )

    candidates = df[base_mask & (desk_mask | is_sticky_series)].copy()
    candidates["passes_desk"] = desk_mask.loc[candidates.index]
    candidates["is_sticky"] = candidates["type_id"].isin(desk_sticky_ids)

    max_cap_m = (cfg.max_line_cap * scale_factor) / 1e6
    deployable_m = (candidates["daily_turnover_m"] * cfg.vol_pct).clip(upper=max_cap_m)
    net_spread = (candidates["avg_spread_30d"] - scfg.edge_baseline_pct).clip(lower=0.0)
    cand_swing_edge = (swing_amp_30d.loc[candidates.index] - scfg.edge_baseline_pct).clip(lower=0.0)
    effective_edge = net_spread.combine(cand_swing_edge, max).clip(upper=scfg.max_effective_edge_pct)

    if desk_type == "hold":
        raw_upside = (
            ((candidates["price_m_180d"] * scfg.hold_target_vwap180_mult) - candidates["price_m_30d"])
            / candidates["price_m_30d"]
            * 100.0
        )
        inelastic_bonus = 1.0 + (1.0 - candidates["supply_elasticity"])
        candidates["upside_pct"] = (
            raw_upside
            * inelastic_bonus
            * (1.0 + candidates["daily_turnover_m"].clip(lower=0.0).map(math.log1p))
        )
    elif desk_type == "tactical":
        candidates["swing_score"] = (
            candidates["dump_freq_pct"]
            * effective_edge
            * (1.0 + deployable_m.clip(lower=0.0).map(math.log1p))
        )
    else:
        elast_w = scfg.profit_velocity_elasticity_weight
        candidates["profit_velocity"] = (
            deployable_m * net_spread * ((1.0 - elast_w) + elast_w * candidates["supply_elasticity"])
        )

    candidates = candidates.sort_values(by=["is_sticky", cfg.sort_col], ascending=[False, False])

    params = {
        "vol_pct": cfg.vol_pct,
        "max_line_cap": cfg.max_line_cap * scale_factor,
        "min_line_cap": cfg.min_line_cap,
        "min_margin_req": cfg.min_margin,
        "sticky_margin_floor": cfg.sticky_margin_floor,
    }
    return candidates, params


def v2_compute_candidate_pricing(
    row: pd.Series,
    desk_type: str,
    p_info: dict,
    incumbents: IncumbentState,
    is_sticky: bool,
) -> dict | None:
    """
    V2 pricing uses `effective_clearing_ask` (ignoring thin <0.35d tourist lowballs)
    so transient lowball asks do not depress target exits or distort margins.
    """
    scfg = Desk.get_desk_profile(desk_type).selection
    effective_p_info = dict(p_info or {})
    if effective_p_info.get("is_thin_lowball") and effective_p_info.get("effective_clearing_ask", 0.0) > 0:
        effective_p_info["lowest_ask"] = effective_p_info["effective_clearing_ask"]
    vwap_30d_isk = float(row.get("price_m_30d") or 0.0) * 1e6
    if vwap_30d_isk > 0 and effective_p_info.get("lowest_ask", 0.0) > (vwap_30d_isk * scfg.ask_cap_vwap30_mult):
        effective_p_info["lowest_ask"] = round(vwap_30d_isk * scfg.ask_cap_vwap30_mult, 2)
    return _base_compute_candidate_pricing(row, desk_type, effective_p_info, incumbents, is_sticky)

def v2_evaluate_order_action(
    desk_type: str,
    my_price: float,
    top_buy: float,
    t_sell: float,
    dump_freq: float,
    proximity_pct: float = None,
    csv_sell: float = 0.0,
    live_ask: float = None,
    ask_days_of_supply: float | None = None,
    supply_class: str = "MANUFACTURED",
) -> tuple[str, str]:
    """
    Evaluates whether an active order is winning, viable to bump, or needs cancellation,
    holding firm through thin lowballs on inelastic supply when margin clears cancel_floor.
    """
    cfg = Desk.get_desk_profile(desk_type)
    min_margin = cfg.live_min_margin
    cancel_floor = cfg.cancel_floor

    if (
        ask_days_of_supply is not None
        and ask_days_of_supply < 0.40
        and supply_class in ("LP_FACTION", "FARMED_RNG")
        and csv_sell > 0
    ):
        base_margin = calc_projected_margin(my_price, csv_sell)
        if base_margin >= cancel_floor:
            if not top_buy or top_buy <= 0:
                return "PATIENT", "AWAITING QUOTE"
            if my_price >= (top_buy - 0.01):
                return "TOP_BOOK", f"TOP BID (THIN SUPPLY {ask_days_of_supply:.2f}d)"
            return "PATIENT", f"HOLD THIN SUPPLY ({ask_days_of_supply:.2f}d ASK)"

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


def v2_evaluate_sell_collision(
    desk: str,
    qty: int,
    unit_cost: float,
    lowest_ask: float | None,
    active_orders: list[dict],
    hold_meta: dict,
    ask_days_of_supply: float | None = None,
    supply_class: str = "MANUFACTURED",
) -> tuple[str, str, float, dict]:
    """
    V2 sell-collision evaluation: refuses to undercut 1-2 unit tourist lowballs
    (<0.35 days of daily volume) when supply is inelastic (LP_FACTION / FARMED_RNG).
    """
    from workflows.sell_advisor import _resolve_collision_strategy

    min_active_price = min((o["price"] for o in active_orders), default=0.0)
    active_qty = sum(o["volume_remain"] for o in active_orders)
    is_undercut = bool(lowest_ask and lowest_ask < (min_active_price - eve_tick(lowest_ask)))

    if (
        is_undercut
        and ask_days_of_supply is not None
        and ask_days_of_supply < 0.35
        and supply_class in ("LP_FACTION", "FARMED_RNG")
    ):
        return (
            "HOLD WALL (THIN LOWBALL)",
            "hold",
            round(min_active_price, 2),
            {
                "has_collision": True,
                "active_qty": active_qty,
                "active_price": min_active_price,
                "active_ratio": round((active_qty / max(1, active_qty + qty)) * 100.0, 1),
                "is_undercut": True,
                "action": "HOLD_HANGAR",
                "badge": f"🧱 THIN LOWBALL ({ask_days_of_supply:.2f}d)",
                "reason": f"Only {ask_days_of_supply:.2f}d of supply ahead at {lowest_ask:,.2f} on {supply_class}. Hold wall @ {min_active_price:,.2f}.",
            },
        )

    return _resolve_collision_strategy(desk, qty, unit_cost, lowest_ask, active_orders, hold_meta)