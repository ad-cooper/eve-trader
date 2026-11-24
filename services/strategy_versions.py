from __future__ import annotations
from dataclasses import dataclass
import json
import math
import os
import sqlite3
from typing import Callable
import pandas as pd

from services.accounting import (
    eve_tick,
    calc_projected_margin,
)
from services.ledgers import (
    IncumbentState,
    get_desk_profile,
    select_desk_candidates as v1_select_desk_candidates,
    compute_candidate_pricing as v1_compute_candidate_pricing,
    compute_line_sizing as v1_compute_line_sizing,
)

STRATEGY_CONFIG_FILE = "data/strategy_config.json"
DEFAULT_ACTIVE_VERSION = "v2"


@dataclass(frozen=True)
class StrategyVersion:
    """
    Pluggable strategy definition allowing v1, v2, and future AI-proposed versions
    to run side-by-side in shadow mode and be compared in retrospective backtests.
    """
    version: str
    description: str
    enrich_universe: Callable[[pd.DataFrame, str, str], pd.DataFrame]
    select_candidates: Callable[[pd.DataFrame, str, pd.Series, set[int], float], tuple[pd.DataFrame, dict]]
    price_candidate: Callable[[pd.Series, str, dict, IncumbentState, bool], dict | None]
    size_line: Callable[[pd.Series, dict, dict, IncumbentState, bool, bool, float], dict | None]
    evaluate_buy_order: Callable[..., tuple[str, str]]
    evaluate_sell_collision: Callable[..., tuple[str, str, float, dict]]


STRATEGY_REGISTRY: dict[str, StrategyVersion] = {}


def register_strategy_version(spec: StrategyVersion) -> StrategyVersion:
    """Registers a strategy engine version (e.g. 'v1', 'v2', 'ai-20261125')."""
    STRATEGY_REGISTRY[spec.version.strip().lower()] = spec
    return spec


def get_strategy_version(version: str | None = None) -> StrategyVersion:
    key = str(version or get_active_strategy_version()).strip().lower()
    if key not in STRATEGY_REGISTRY:
        raise KeyError(f"Unknown strategy version '{key}'. Registered: {sorted(STRATEGY_REGISTRY.keys())}")
    return STRATEGY_REGISTRY[key]


def list_strategy_versions() -> list[str]:
    return sorted(STRATEGY_REGISTRY.keys())


def get_active_strategy_version() -> str:
    env_v = os.environ.get("EVE_STRATEGY_VERSION", "").strip().lower()
    if env_v and env_v in STRATEGY_REGISTRY:
        return env_v
    if os.path.exists(STRATEGY_CONFIG_FILE):
        try:
            with open(STRATEGY_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            v = str(cfg.get("active_version", DEFAULT_ACTIVE_VERSION)).strip().lower()
            if v in STRATEGY_REGISTRY:
                return v
        except Exception:
            pass
    return DEFAULT_ACTIVE_VERSION


def set_active_strategy_version(version: str) -> str:
    key = str(version).strip().lower()
    if key not in STRATEGY_REGISTRY:
        raise ValueError(f"Cannot activate unknown strategy version '{key}'. Available: {list_strategy_versions()}")
    os.makedirs(os.path.dirname(STRATEGY_CONFIG_FILE), exist_ok=True)
    with open(STRATEGY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"active_version": key}, f, indent=2)
    return key


def get_version_output_dir(version: str, base_dir: str = "ledgers") -> str:
    """
    Returns 'ledgers' for the active production version so live UI/API reads it directly,
    and 'ledgers/shadow/<version>' for shadow/experimental versions running side-by-side.
    """
    key = str(version).strip().lower()
    if key == get_active_strategy_version():
        return base_dir
    return os.path.join(base_dir, "shadow", key)


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
      - natural_cycle_days: intrinsic restock half-life in days (3d / 8d / 16d)
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
# V1 Engine Wrapper (Current Production Pipeline)
# ==============================================================================

def _v1_passthrough_enrich(df: pd.DataFrame, sqlite_path: str = "", duckdb_path: str = "") -> pd.DataFrame:
    out = df.copy()
    if "supply_class" not in out.columns:
        out["supply_class"] = "LEGACY_V1"
    if "natural_cycle_days" not in out.columns:
        out["natural_cycle_days"] = 5.0
    if "supply_elasticity" not in out.columns:
        out["supply_elasticity"] = 0.50
    return out


def _v1_buy_order_wrapper(*args, **kwargs):
    from services.ledgers import evaluate_order_action
    return evaluate_order_action(*args, **kwargs)


def _v1_sell_collision_wrapper(*args, **kwargs):
    from workflows.sell_advisor import _resolve_collision_strategy
    return _resolve_collision_strategy(*args, **kwargs)


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
    cfg = get_desk_profile(desk_type)
    if "supply_class" not in df.columns:
        df = enrich_universe_fundamentals(df)

    is_sticky_series = df["type_id"].isin(desk_sticky_ids)
    s_class = df["supply_class"]

    p20_safe = df["p20_low_m_30d"].fillna(df["price_m_30d"]).replace(0, float("nan"))
    swing_amp_30d = (((df["price_m_30d"] - p20_safe) / p20_safe) * 100.0).fillna(0.0)
    spread_or_swing = (df["avg_spread_30d"] >= cfg.min_spread_pct) | (
        (desk_type in ("tactical", "hold")) & (swing_amp_30d >= 14.0)
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

    active_days = df["active_days_30d"].fillna(30) if "active_days_30d" in df.columns else pd.Series(30, index=df.index)

    if desk_type == "evergreen":
        desk_mask &= (
            (df["daily_vol_30d"] >= cfg.min_daily_vol)
            & (active_days >= 20)
            & ((s_class == "MANUFACTURED") | (df["daily_turnover_m"] >= 120.0))
        )
    elif desk_type == "tactical":
        desk_mask &= (
            (df["daily_vol_30d"] >= 1.5)
            & (active_days >= 15)
            & (df["dump_freq_pct"] >= max(22.0, cfg.min_dump_freq_pct - 5.0))
            & (s_class.isin(["LP_FACTION", "FARMED_RNG"]) | (df["dump_freq_pct"] >= 45.0))
        )
    else:  # hold
        ratio_cap = cfg.max_vwap_ratio_180d if cfg.max_vwap_ratio_180d is not None else 0.90
        inelastic_hold = (df["price_m_30d"] <= (df["price_m_180d"] * ratio_cap)) & s_class.isin(
            ["FARMED_RNG", "LP_FACTION"]
        )
        deep_mfg_hold = (df["price_m_30d"] <= (df["price_m_180d"] * min(0.85, ratio_cap - 0.05))) & (
            s_class == "MANUFACTURED"
        )
        desk_mask &= (df["daily_vol_30d"] >= 1.0) & (active_days >= 12) & (inelastic_hold | deep_mfg_hold)

    candidates = df[base_mask & (desk_mask | is_sticky_series)].copy()
    candidates["passes_desk"] = desk_mask.loc[candidates.index]
    candidates["is_sticky"] = candidates["type_id"].isin(desk_sticky_ids)

    max_cap_m = (cfg.max_line_cap * scale_factor) / 1e6
    deployable_m = (candidates["daily_turnover_m"] * cfg.vol_pct).clip(upper=max_cap_m)
    net_spread = (candidates["avg_spread_30d"] - 5.35).clip(lower=0.0)
    cand_swing_edge = (swing_amp_30d.loc[candidates.index] - 5.35).clip(lower=0.0)
    effective_edge = net_spread.combine(cand_swing_edge, max).clip(upper=45.0)

    if desk_type == "hold":
        raw_upside = (
            ((candidates["price_m_180d"] * 0.96) - candidates["price_m_30d"])
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
        candidates["profit_velocity"] = deployable_m * net_spread * (0.7 + 0.3 * candidates["supply_elasticity"])

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
    effective_p_info = dict(p_info or {})
    if effective_p_info.get("is_thin_lowball") and effective_p_info.get("effective_clearing_ask", 0.0) > 0:
        effective_p_info["lowest_ask"] = effective_p_info["effective_clearing_ask"]
    vwap_30d_isk = float(row.get("price_m_30d") or 0.0) * 1e6
    if vwap_30d_isk > 0 and effective_p_info.get("lowest_ask", 0.0) > (vwap_30d_isk * 1.40):
        effective_p_info["lowest_ask"] = round(vwap_30d_isk * 1.40, 2)
    return v1_compute_candidate_pricing(row, desk_type, effective_p_info, incumbents, is_sticky)


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
    V2 buy-order evaluation: if live ask dipped only because of a thin lowball queue
    (<0.40 days of supply) on LP_FACTION / FARMED_RNG items, holds firm instead of cancelling.
    """
    from services.ledgers import evaluate_order_action as v1_eval

    if (
        ask_days_of_supply is not None
        and ask_days_of_supply < 0.40
        and supply_class in ("LP_FACTION", "FARMED_RNG")
        and csv_sell > 0
    ):
        base_margin = calc_projected_margin(my_price, csv_sell)
        cfg = get_desk_profile(desk_type)
        if base_margin >= cfg.cancel_floor:
            if not top_buy or top_buy <= 0:
                return "PATIENT", "AWAITING QUOTE"
            if my_price >= (top_buy - 0.01):
                return "TOP_BOOK", f"TOP BID (THIN SUPPLY {ask_days_of_supply:.2f}d)"
            return "PATIENT", f"HOLD THIN SUPPLY ({ask_days_of_supply:.2f}d ASK)"

    return v1_eval(desk_type, my_price, top_buy, t_sell, dump_freq, proximity_pct, csv_sell, live_ask)


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
    from workflows.sell_advisor import _resolve_collision_strategy as v1_col

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

    return v1_col(desk, qty, unit_cost, lowest_ask, active_orders, hold_meta)


register_strategy_version(
    StrategyVersion(
        version="v1",
        description="Production multi-desk engine (30d/180d VWAP + sticky incumbents + weekend hysteresis)",
        enrich_universe=_v1_passthrough_enrich,
        select_candidates=v1_select_desk_candidates,
        price_candidate=v1_compute_candidate_pricing,
        size_line=v1_compute_line_sizing,
        evaluate_buy_order=_v1_buy_order_wrapper,
        evaluate_sell_collision=_v1_sell_collision_wrapper,
    )
)

register_strategy_version(
    StrategyVersion(
        version="v2",
        description="Fundamental microstructure engine (SDE supply elasticity + book-depth lowball filtering)",
        enrich_universe=enrich_universe_fundamentals,
        select_candidates=v2_select_desk_candidates,
        price_candidate=v2_compute_candidate_pricing,
        size_line=v1_compute_line_sizing,
        evaluate_buy_order=v2_evaluate_order_action,
        evaluate_sell_collision=v2_evaluate_sell_collision,
    )
)
