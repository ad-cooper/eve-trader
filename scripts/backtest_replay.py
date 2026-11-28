#!/usr/bin/env python3
"""
Point-in-Time Walk-Forward Replay & Missed-Winner Hindsight Auditor (v2)
========================================================================
1. Steps backward through historical dates in DuckDB (`forge_market_history`)
   with strict zero-lookahead point-in-time metric slicing (`date <= as_of_date`).
2. Runs the `v2` multi-desk waterfall on identical historical universes and
   simulates day-by-day chronological fills, target exits, take-profits, and
   time-stops.
3. Audits the entire Jita universe in hindsight to surface the highest-profit
   trades that occurred during the window and pinpoints the exact rule that
   blocked each winner in `v2`.
"""

from __future__ import annotations
import os
import sys
import argparse
import sqlite3
import datetime
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.accounting import (
    calc_unit_cost,
    calc_net_proceeds,
    calc_projected_margin,
)
from services.exclusions import load_active_blacklist
from services.desk_config import (
    DESK_PROFILES,
)
from services.desk import (
    Desk,
    IncumbentState,
)
from services.market_universe import build_universe_exclusion_mask
from services.strategy_versions import (
    enrich_universe_fundamentals,
    v2_compute_candidate_pricing,
    v2_compute_line_sizing,
    v2_select_desk_candidates,
)

from constants import (
    DESK_ORDER,
    DUCKDB_PATH,
    SQLITE_PATH,
)

DESK_RATIOS = {k: cfg.ratio for k, cfg in DESK_PROFILES.items()}


def load_sde_item_metadata(sqlite_path: str = SQLITE_PATH) -> pd.DataFrame:
    """Loads published marketable item metadata from SDE SQLite once for fast point-in-time joins."""
    if not sqlite_path or not os.path.exists(sqlite_path):
        return pd.DataFrame(columns=["type_id", "item_name", "group_name", "category_name"])

    con = sqlite3.connect(sqlite_path)
    query = """
    SELECT
        t.typeID AS type_id,
        t.typeName AS item_name,
        g.groupName AS group_name,
        c.categoryName AS category_name
    FROM invTypes t
    JOIN invGroups g ON t.groupID = g.groupID
    JOIN invCategories c ON g.categoryID = c.categoryID
    WHERE t.published = 1
      AND t.marketGroupID IS NOT NULL
      AND COALESCE(t.marketGroupID, 0) NOT IN (2487, 1661)
      AND LOWER(COALESCE(t.description, '')) NOT LIKE '%cease to function%'
      AND LOWER(COALESCE(t.description, '')) NOT LIKE '%will expire%'
      AND LOWER(COALESCE(t.description, '')) NOT LIKE '%expired%';
    """
    df_meta = pd.read_sql_query(query, con)
    con.close()
    return df_meta


def build_point_in_time_universe(
    con: duckdb.DuckDBPyConnection,
    as_of_date: datetime.date | str,
    sde_meta_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Computes 180d, 30d, 7d, and floor metrics strictly from `forge_market_history`
    on or before `as_of_date` (zero look-ahead leakage).
    """
    date_str = str(as_of_date)[:10]
    query = f"""
    WITH ref AS (
        SELECT CAST('{date_str}' AS DATE) AS cutoff
    ),
    metrics_180d AS (
        SELECT
            h.type_id,
            COUNT(*) AS total_history_days,
            MAX(h.average) AS high_180d,
            SUM(h.average * h.volume) / NULLIF(SUM(h.volume), 0) AS vwap_180d
        FROM forge_market_history h, ref
        WHERE h.date > ref.cutoff - INTERVAL 180 DAY
          AND h.date <= ref.cutoff
          AND h.volume > 0
        GROUP BY h.type_id
    ),
    metrics_30d AS (
        SELECT
            h.type_id,
            COUNT(*) AS active_days_30d,
            AVG(h.volume) AS daily_vol_30d,
            SUM(h.average * h.volume) / NULLIF(SUM(h.volume), 0) AS vwap_30d,
            AVG((h.highest - h.lowest) / NULLIF(h.average, 0)) * 100.0 AS avg_spread_30d,
            (COUNT(CASE WHEN (h.average - h.lowest) / NULLIF(h.average, 0) >= 0.045 THEN 1 END) * 100.0 / COUNT(*)) AS dump_freq_pct
        FROM forge_market_history h, ref
        WHERE h.date > ref.cutoff - INTERVAL 30 DAY
          AND h.date <= ref.cutoff
          AND h.volume > 0
        GROUP BY h.type_id
    ),
    metrics_7d AS (
        SELECT
            h.type_id,
            SUM(h.average * h.volume) / NULLIF(SUM(h.volume), 0) AS vwap_7d,
            MIN(h.lowest) AS min_low_7d
        FROM forge_market_history h, ref
        WHERE h.date > ref.cutoff - INTERVAL 7 DAY
          AND h.date <= ref.cutoff
          AND h.volume > 0
        GROUP BY h.type_id
    ),
    metrics_floors AS (
        SELECT
            h.type_id,
            QUANTILE_CONT(CASE WHEN h.date > ref.cutoff - INTERVAL 14 DAY THEN h.lowest END, 0.25) AS min_low_14d,
            QUANTILE_CONT(h.lowest, 0.20) AS p20_low_30d
        FROM forge_market_history h, ref
        WHERE h.date > ref.cutoff - INTERVAL 30 DAY
          AND h.date <= ref.cutoff
          AND h.volume > 0
        GROUP BY h.type_id
    )
    SELECT
        m.type_id,
        l.total_history_days,
        m.active_days_30d,
        l.vwap_180d / 1e6 AS price_m_180d,
        m.vwap_30d / 1e6 AS price_m_30d,
        w.vwap_7d / 1e6 AS price_m_7d,
        f.min_low_14d / 1e6 AS min_low_m_14d,
        f.p20_low_30d / 1e6 AS p20_low_m_30d,
        m.daily_vol_30d,
        m.dump_freq_pct,
        m.avg_spread_30d,
        (m.daily_vol_30d * (m.vwap_30d / 1e6)) AS daily_turnover_m
    FROM metrics_30d m
    JOIN metrics_7d w ON m.type_id = w.type_id
    JOIN metrics_floors f ON m.type_id = f.type_id
    JOIN metrics_180d l ON m.type_id = l.type_id;
    """
    df_metrics = con.execute(query).df()
    if df_metrics.empty or sde_meta_df.empty:
        return pd.DataFrame()
    return df_metrics.merge(sde_meta_df, on="type_id", how="inner")


def fetch_forward_bars_map(
    con: duckdb.DuckDBPyConnection,
    as_of_date: datetime.date | str,
    horizon_days: int,
    type_ids: list[int] | None = None,
) -> dict[int, list[dict]]:
    """Loads ordered daily OHLCV bars strictly after `as_of_date` up to `as_of_date + horizon_days`."""
    date_str = str(as_of_date)[:10]
    id_filter = ""
    if type_ids:
        clean_ids = ",".join(str(int(t)) for t in set(type_ids))
        id_filter = f"AND type_id IN ({clean_ids})"

    query = f"""
    SELECT
        type_id,
        date,
        average,
        highest,
        lowest,
        volume
    FROM forge_market_history
    WHERE date > CAST('{date_str}' AS DATE)
      AND date <= CAST('{date_str}' AS DATE) + INTERVAL {int(horizon_days)} DAY
      {id_filter}
    ORDER BY type_id ASC, date ASC;
    """
    df_bars = con.execute(query).df()
    bars_map: dict[int, list[dict]] = {}
    if df_bars.empty:
        return bars_map

    for tid, grp in df_bars.groupby("type_id", sort=False):
        bars_map[int(tid)] = grp[["date", "average", "highest", "lowest", "volume"]].to_dict(orient="records")
    return bars_map


def simulate_trade_trajectory(
    future_bars: list[dict],
    qty: int,
    target_buy: float,
    target_sell: float,
    max_hold_days: int,
    take_profit_days: int = 0,
    take_profit_roi: float = 2.0,
    buy_window_days: int | None = None,
    fill_participation: float = 0.25,
) -> dict:
    """
    Chronologically simulates a limit buy order placed after close on day 0 across subsequent
    daily OHLCV bars (`date`, `average`, `highest`, `lowest`, `volume`).
    """
    if qty <= 0 or target_buy <= 0 or target_sell <= 0 or not future_bars:
        return {
            "status": "UNFILLED",
            "filled_qty": 0,
            "sold_qty": 0,
            "fill_day": None,
            "exit_day": None,
            "deployed_capital": 0.0,
            "realized_pnl": 0.0,
            "mtm_pnl": 0.0,
            "roi_pct": 0.0,
        }

    buy_cutoff = buy_window_days if buy_window_days is not None else max(2, min(7, max_hold_days // 2 or 2))
    unit_cost = calc_unit_cost(target_buy)

    filled_qty = 0
    sold_qty = 0
    first_fill_day: int | None = None
    last_exit_day: int | None = None
    total_net_proceeds = 0.0
    status = "UNFILLED"

    for day_idx, bar in enumerate(future_bars, start=1):
        low = float(bar.get("lowest") or 0.0)
        high = float(bar.get("highest") or 0.0)
        avg = float(bar.get("average") or 0.0)
        vol = int(bar.get("volume") or 0)

        if vol <= 0:
            continue

        newly_filled_today = 0
        if day_idx <= buy_cutoff and filled_qty < qty and low > 0 and low <= target_buy:
            part = fill_participation if low < target_buy else (fill_participation * 0.6)
            cap_today = max(1, int(vol * part))
            newly_filled_today = min(qty - filled_qty, cap_today)
            filled_qty += newly_filled_today
            if first_fill_day is None:
                first_fill_day = day_idx

        held_qty = filled_qty - sold_qty
        if held_qty <= 0 or first_fill_day is None:
            continue

        eligible_to_sell = held_qty if day_idx > first_fill_day else max(0, held_qty - newly_filled_today)
        days_held = day_idx - first_fill_day

        if eligible_to_sell > 0 and high >= target_sell:
            sell_cap = max(1, int(vol * fill_participation))
            sell_now = min(eligible_to_sell, sell_cap)
            sold_qty += sell_now
            total_net_proceeds += sell_now * calc_net_proceeds(target_sell)
            last_exit_day = day_idx
            if sold_qty >= qty and filled_qty >= qty:
                status = "TARGET_HIT"
                break
            elif sold_qty == filled_qty and day_idx > buy_cutoff:
                status = "TARGET_HIT"
                break
            continue

        if eligible_to_sell > 0 and avg > 0:
            late_cycle_days = max(take_profit_days + 1, int(max_hold_days * 0.65))
            avg_net = calc_net_proceeds(avg)
            live_roi = ((avg_net - unit_cost) / unit_cost) * 100.0
            tp_triggered = (
                (take_profit_days > 0 and days_held >= take_profit_days and live_roi >= take_profit_roi)
                or (days_held >= late_cycle_days and live_roi >= 2.0)
            )
            if tp_triggered:
                sell_cap = max(1, int(vol * fill_participation))
                sell_now = min(eligible_to_sell, sell_cap)
                sold_qty += sell_now
                total_net_proceeds += sell_now * avg_net
                last_exit_day = day_idx
                if sold_qty == filled_qty and day_idx >= buy_cutoff:
                    status = "TAKE_PROFIT"
                    break
                continue

        if days_held >= max_hold_days and eligible_to_sell > 0:
            liq_price = avg if avg > 0 else (low if low > 0 else target_buy)
            sold_qty += eligible_to_sell
            total_net_proceeds += eligible_to_sell * calc_net_proceeds(liq_price)
            last_exit_day = day_idx
            status = "TIME_STOP"
            break

    if filled_qty == 0:
        return {
            "status": "UNFILLED",
            "filled_qty": 0,
            "sold_qty": 0,
            "fill_day": None,
            "exit_day": None,
            "deployed_capital": 0.0,
            "realized_pnl": 0.0,
            "mtm_pnl": 0.0,
            "roi_pct": 0.0,
        }

    deployed_capital = filled_qty * unit_cost
    unsold_qty = filled_qty - sold_qty
    last_avg = float(future_bars[-1].get("average") or target_buy)
    mtm_proceeds = unsold_qty * calc_net_proceeds(last_avg)

    if status == "UNFILLED":
        status = "OPEN_MTM" if unsold_qty > 0 else "TARGET_HIT"

    realized_pnl = (total_net_proceeds - (sold_qty * unit_cost)) if sold_qty > 0 else 0.0
    total_pnl = (total_net_proceeds + mtm_proceeds) - deployed_capital
    roi_pct = (total_pnl / deployed_capital) * 100.0 if deployed_capital > 0 else 0.0

    return {
        "status": status,
        "filled_qty": filled_qty,
        "sold_qty": sold_qty,
        "fill_day": first_fill_day,
        "exit_day": last_exit_day,
        "deployed_capital": round(deployed_capital, 2),
        "realized_pnl": round(realized_pnl, 2),
        "mtm_pnl": round(total_pnl, 2),
        "roi_pct": round(roi_pct, 2),
    }


def simulate_v2_on_date(
    df_pit: pd.DataFrame,
    total_budget: float,
    bl_items: set[str],
    bl_groups: set[str],
    bl_meta: dict,
    sqlite_path: str = SQLITE_PATH,
    duckdb_path: str = DUCKDB_PATH,
) -> tuple[list[dict], pd.DataFrame]:
    """
    Runs a clean-slate v2 multi-desk waterfall on a single historical point-in-time
    universe snapshot `df_pit` (zero future telemetry or live order leakage).
    """
    df_enriched = enrich_universe_fundamentals(df_pit, sqlite_path, duckdb_path)

    empty_incumbents = IncumbentState(
        active_buys={},
        active_sells={},
        asset_qtys={},
        significant_assets=set(),
        desk_sticky_ids=set(),
    )
    scale_factor = max(1.0, min(2.5, total_budget / 50.0e9))
    claimed_ids: set[int] = set()
    carried_slack = 0.0
    picks: list[dict] = []

    for desk in DESK_ORDER:
        target_budget = (total_budget * DESK_RATIOS[desk]) + carried_slack
        base_mask = build_universe_exclusion_mask(
            df=df_enriched,
            claimed_ids=claimed_ids,
            incumbents=empty_incumbents,
            bl_items=bl_items,
            bl_groups=bl_groups,
            bl_meta=bl_meta,
            losing_tids=None,
        )
        candidates, desk_params = v2_select_desk_candidates(
            df_enriched,
            desk,
            base_mask,
            set(),
            scale_factor,
        )

        allocated_cap = 0.0
        for _, row in candidates.iterrows():
            remaining_budget = target_budget - allocated_cap
            if remaining_budget <= 0:
                break

            tid = int(row["type_id"])
            pricing = v2_compute_candidate_pricing(row, desk, {}, empty_incumbents, False)
            if pricing is None:
                continue

            sizing = v2_compute_line_sizing(
                row,
                pricing,
                desk_params,
                empty_incumbents,
                False,
                bool(row.get("passes_desk", True)),
                remaining_budget,
            )
            if sizing is None:
                continue

            allocated_cap += sizing["capital"]
            claimed_ids.add(tid)
            picks.append({
                "desk_type": desk,
                "type_id": tid,
                "name": row["item_name"],
                "supply_class": str(row.get("supply_class") or "UNKNOWN"),
                "natural_cycle_days": float(row.get("natural_cycle_days") or Desk.get_desk_profile(desk).max_hold_days),
                "qty": int(sizing["qty"]),
                "target_buy": round(float(pricing["target_buy"]), 2),
                "target_sell": round(float(pricing["target_sell"]), 2),
                "projected_margin": round(float(pricing["net_margin"]), 2),
                "planned_capital": round(float(sizing["capital"]), 2),
                "projected_profit": round(float(sizing["profit"]), 2),
            })
            if allocated_cap >= (target_budget * 0.995):
                break

        carried_slack = max(0.0, target_budget - allocated_cap)

    return picks, df_enriched


def diagnose_rejection_reason(
    row: pd.Series,
    desk: str,
    base_mask_val: bool,
    picked_ids_by_desk: dict[str, set[int]],
) -> str:
    """Pinpoints the exact first v2 rule that prevented `row` from being selected on `desk`."""
    tid = int(row["type_id"])
    for upstream in DESK_ORDER:
        if upstream == desk:
            break
        if tid in picked_ids_by_desk.get(upstream, set()):
            return f"CLAIMED_BY_{upstream.upper()}"

    if tid in picked_ids_by_desk.get(desk, set()):
        return "PICKED"

    if not base_mask_val:
        if float(row["price_m_7d"]) < float(row["price_m_30d"]) * 0.78:
            return "EXCLUDED_7D_CRASH_GUARD"
        return "EXCLUDED_BLACKLIST_OR_CATEGORY"

    cfg = Desk.get_desk_profile(desk)
    scfg = cfg.selection
    price_val = float(row[cfg.price_col])
    if not (cfg.min_price_m <= price_val <= cfg.max_price_m):
        return "PRICE_OUT_OF_RANGE"
    if float(row["daily_turnover_m"]) < cfg.min_turnover_m:
        return "TURNOVER_TOO_LOW"
    p20_val = float(row.get("p20_low_m_30d") or 0.0)
    swing_amp = ((float(row["price_m_30d"]) - p20_val) / p20_val * 100.0) if p20_val > 0 else 0.0
    if float(row["avg_spread_30d"]) < cfg.min_spread_pct and not (
        desk in ("tactical", "hold") and swing_amp >= scfg.swing_amp_min_pct
    ):
        return "SPREAD_TOO_NARROW"
    if cfg.min_history_days > 0 and int(row["total_history_days"]) < cfg.min_history_days:
        return "INSUFFICIENT_HISTORY"

    s_class = str(row.get("supply_class") or "UNKNOWN")
    if desk == "evergreen":
        if cfg.min_daily_vol > 0 and float(row["daily_vol_30d"]) < cfg.min_daily_vol:
            return "DAILY_VOL_TOO_LOW"
        if not (s_class == "MANUFACTURED" or float(row["daily_turnover_m"]) >= scfg.turnover_archetype_floor_m):
            return "V2_SUPPLY_CLASS_FILTERED"
    elif desk == "tactical":
        min_df = max(scfg.min_dump_freq_floor_pct, cfg.min_dump_freq_pct - scfg.dump_freq_floor_offset_pct)
        if float(row["dump_freq_pct"]) < min_df:
            return "DUMP_FREQ_TOO_LOW"
        if not (s_class in ("LP_FACTION", "FARMED_RNG") or float(row["dump_freq_pct"]) >= scfg.non_archetype_min_dump_freq_pct):
            return "V2_SUPPLY_CLASS_FILTERED"
    else:  # hold
        ratio_cap = cfg.max_vwap_ratio_180d if cfg.max_vwap_ratio_180d is not None else 0.90
        if float(row["price_m_30d"]) > float(row["price_m_180d"]) * ratio_cap:
            return "DISCOUNT_TO_180D_TOO_SHALLOW"
        if s_class not in ("FARMED_RNG", "LP_FACTION"):
            return "V2_SUPPLY_CLASS_FILTERED"

    empty_inc = IncumbentState({}, {}, {}, set(), set())
    pricing = v2_compute_candidate_pricing(row, desk, {}, empty_inc, False)
    if pricing is None or float(pricing["net_margin"]) < cfg.min_margin:
        return "NET_MARGIN_BELOW_FLOOR"

    return "RANKED_BELOW_BUDGET_CUTOFF"


def audit_missed_winners(
    df_pit_enriched: pd.DataFrame,
    bars_map: dict[int, list[dict]],
    as_of_date: str,
    picks: list[dict],
    bl_items: set[str],
    bl_groups: set[str],
    bl_meta: dict,
    min_profit_isk: float = 15_000_000.0,
) -> list[dict]:
    """
    Scans the entire eligible point-in-time Jita universe to find the most profitable
    trades that occurred over `bars_map` and diagnoses why `v2` missed them.
    """
    empty_inc = IncumbentState({}, {}, {}, set(), set())
    base_mask = build_universe_exclusion_mask(
        df=df_pit_enriched,
        claimed_ids=set(),
        incumbents=empty_inc,
        bl_items=bl_items,
        bl_groups=bl_groups,
        bl_meta=bl_meta,
        losing_tids=None,
    )

    picked_by_desk: dict[str, set[int]] = {d: set() for d in DESK_ORDER}
    picked_union: set[int] = set()
    for p in picks:
        picked_by_desk[p["desk_type"]].add(int(p["type_id"]))
        picked_union.add(int(p["type_id"]))

    missed_records: list[dict] = []
    scan_df = df_pit_enriched[base_mask & (df_pit_enriched["daily_turnover_m"] >= 10.0)]

    for idx, row in scan_df.iterrows():
        tid = int(row["type_id"])
        if tid in picked_union:
            continue
        future_bars = bars_map.get(tid)
        if not future_bars:
            continue

        vwap_30d = float(row["price_m_30d"]) * 1e6
        vwap_180d = float(row["price_m_180d"]) * 1e6
        p20_low = float(row.get("p20_low_m_30d") or vwap_30d * 0.92) * 1e6
        if vwap_30d <= 0:
            continue

        if vwap_30d <= vwap_180d * 0.92 and int(row["total_history_days"]) >= 60:
            bench_desk = "hold"
            t_sell = min(vwap_180d * 0.96, vwap_30d * 1.22)
        elif float(row["dump_freq_pct"]) >= 30.0:
            bench_desk = "tactical"
            t_sell = vwap_30d * 1.07
        else:
            bench_desk = "evergreen"
            t_sell = vwap_30d * 1.055

        t_buy = min(p20_low, vwap_30d * 0.93)
        if calc_projected_margin(t_buy, t_sell) < 6.5:
            continue

        cfg = Desk.get_desk_profile(bench_desk)
        price_m = float(row[cfg.price_col])
        if not (cfg.min_price_m <= price_m <= cfg.max_price_m) or float(row["daily_vol_30d"]) < 2.0:
            continue
        unit_cost = calc_unit_cost(t_buy)
        if unit_cost <= 0 or unit_cost > cfg.max_line_cap:
            continue
        qty = min(max(1, int(float(row["daily_vol_30d"]) * cfg.vol_pct)), max(1, int(cfg.max_line_cap / unit_cost)))
        if unit_cost * qty < cfg.min_line_cap:
            continue

        sim = simulate_trade_trajectory(
            future_bars=future_bars,
            qty=qty,
            target_buy=t_buy,
            target_sell=t_sell,
            max_hold_days=cfg.max_hold_days,
            take_profit_days=cfg.take_profit_days,
            take_profit_roi=cfg.take_profit_roi,
        )
        if sim["realized_pnl"] < min_profit_isk:
            continue

        blocker = diagnose_rejection_reason(row, bench_desk, bool(base_mask.loc[idx]), picked_by_desk)
        missed_records.append({
            "as_of_date": as_of_date,
            "desk": bench_desk.upper(),
            "type_id": tid,
            "name": row["item_name"],
            "supply_class": str(row.get("supply_class") or "UNKNOWN"),
            "qty": sim["filled_qty"],
            "buy_price": round(t_buy, 2),
            "sell_price": round(t_sell, 2),
            "deployed_capital": sim["deployed_capital"],
            "realized_pnl": sim["realized_pnl"],
            "roi_pct": sim["roi_pct"],
            "blocker": blocker,
        })

    return missed_records


def run_walkforward_replay(
    con: duckdb.DuckDBPyConnection,
    sde_meta_df: pd.DataFrame,
    days_back: int = 45,
    step_days: int = 5,
    horizon_days: int = 14,
    budget: float = 50.0e9,
    sqlite_path: str = SQLITE_PATH,
    duckdb_path: str = DUCKDB_PATH,
) -> dict:
    """
    Executes a multi-date point-in-time walk-forward replay for `v2` plus a
    hindsight audit of missed winners.
    """
    max_date_row = con.execute("SELECT MAX(date) FROM forge_market_history").fetchone()
    if not max_date_row or max_date_row[0] is None:
        raise RuntimeError("forge_market_history is empty; run scripts/sync_market_data.py first.")

    max_date = pd.to_datetime(max_date_row[0]).date()
    latest_eval_date = max_date - datetime.timedelta(days=horizon_days)
    earliest_eval_date = latest_eval_date - datetime.timedelta(days=max(0, days_back - horizon_days))

    eval_dates: list[datetime.date] = []
    cur = earliest_eval_date
    while cur <= latest_eval_date:
        eval_dates.append(cur)
        cur += datetime.timedelta(days=step_days)
    if not eval_dates:
        eval_dates = [latest_eval_date]

    bl_items, bl_groups, bl_meta = load_active_blacklist()
    trade_logs: list[dict] = []
    missed_all: list[dict] = []

    for dt in eval_dates:
        dt_str = str(dt)
        df_pit = build_point_in_time_universe(con, dt_str, sde_meta_df)
        if df_pit.empty:
            continue

        bars_map = fetch_forward_bars_map(con, dt_str, horizon_days)
        picks, df_enr = simulate_v2_on_date(
            df_pit,
            total_budget=budget,
            bl_items=bl_items,
            bl_groups=bl_groups,
            bl_meta=bl_meta,
            sqlite_path=sqlite_path,
            duckdb_path=duckdb_path,
        )

        for p in picks:
            cfg = Desk.get_desk_profile(p["desk_type"])
            hold_days = int(round(p["natural_cycle_days"]))
            sim = simulate_trade_trajectory(
                future_bars=bars_map.get(p["type_id"], []),
                qty=p["qty"],
                target_buy=p["target_buy"],
                target_sell=p["target_sell"],
                max_hold_days=hold_days,
                take_profit_days=cfg.take_profit_days,
                take_profit_roi=cfg.take_profit_roi,
            )
            trade_logs.append({
                **p,
                "as_of_date": dt_str,
                **sim,
            })

        missed_all.extend(
            audit_missed_winners(
                df_enr,
                bars_map,
                dt_str,
                picks,
                bl_items,
                bl_groups,
                bl_meta,
            )
        )

    return {
        "eval_dates": [str(d) for d in eval_dates],
        "trades": pd.DataFrame(trade_logs),
        "missed": missed_all,
    }


def print_replay_report(report: dict) -> None:
    eval_dates = report["eval_dates"]
    df_trades: pd.DataFrame = report["trades"]
    missed: list[dict] = report["missed"]

    print("\n" + "=" * 96)
    print(f"POINT-IN-TIME WALK-FORWARD REPLAY [V2] ({len(eval_dates)} Decision Dates: {eval_dates[0]} -> {eval_dates[-1]})")
    print("=" * 96)

    if df_trades.empty:
        print("[!] No simulated trades generated in replay window.")
        return

    header = (
        f"{'DESK':<10} | {'ORDERS':>6} | {'FILL %':>6} | {'HIT %':>6} | "
        f"{'TP %':>5} | {'STOP %':>6} | {'DEPLOYED':>10} | {'REALIZED PNL':>12} | {'REALIZED ROI':>12}"
    )
    print(header)
    print("-" * len(header))

    for desk in DESK_ORDER:
        sub = df_trades[df_trades["desk_type"] == desk]
        if sub.empty:
            continue
        n_orders = len(sub)
        filled = sub[sub["filled_qty"] > 0]
        fill_pct = (len(filled) / n_orders) * 100.0 if n_orders > 0 else 0.0
        hit_pct = (len(sub[sub["status"] == "TARGET_HIT"]) / max(1, len(filled))) * 100.0
        tp_pct = (len(sub[sub["status"] == "TAKE_PROFIT"]) / max(1, len(filled))) * 100.0
        stop_pct = (len(sub[sub["status"] == "TIME_STOP"]) / max(1, len(filled))) * 100.0
        deployed = float(filled["deployed_capital"].sum())
        realized = float(filled["realized_pnl"].sum())
        roi = (realized / deployed * 100.0) if deployed > 0 else 0.0
        print(
            f"{desk.upper():<10} | {n_orders:>6} | {fill_pct:>5.1f}% | {hit_pct:>5.1f}% | "
            f"{tp_pct:>4.1f}% | {stop_pct:>5.1f}% | {deployed/1e9:>8.2f}B | {realized/1e9:>+10.2f}B | {roi:>11.2f}%"
        )

    print("-" * len(header))
    filled_all = df_trades[df_trades["filled_qty"] > 0]
    deployed_all = float(filled_all["deployed_capital"].sum())
    realized_all = float(filled_all["realized_pnl"].sum())
    mtm_all = float(filled_all["mtm_pnl"].sum())
    roi_all = (realized_all / deployed_all * 100.0) if deployed_all > 0 else 0.0
    print(
        f"  TOTAL   : {len(filled_all):>4}/{len(df_trades):<4} filled | Deployed: {deployed_all/1e9:>7.2f}B | "
        f"Realized P&L: {realized_all/1e9:>+7.2f}B ({roi_all:>+6.2f}% ROI) | Total MTM P&L: {mtm_all/1e9:>+7.2f}B"
    )

    if not missed:
        print("\n[*] No major missed winners (>15M realized profit) found outside selected portfolios.")
        return

    print("\n" + "=" * 96)
    print("HINDSIGHT MISSED-WINNER RULE DIAGNOSTICS [V2]")
    print("=" * 96)

    blocker_stats: dict[str, list[float]] = {}
    for m in missed:
        r = m.get("blocker", "UNKNOWN")
        blocker_stats.setdefault(r, []).append(m["realized_pnl"])

    for reason, pnls in sorted(blocker_stats.items(), key=lambda x: -sum(x[1]))[:6]:
        print(f"  - {reason:<34}: {len(pnls):>3} opportunities | {sum(pnls)/1e9:>+6.2f}B missed realized profit")

    print("\n=== TOP 15 MISSED PROFIT OPPORTUNITIES (CHRONOLOGICAL SIMULATION) ===")
    m_header = f"{'DATE':<10} | {'DESK':<9} | {'SUPPLY':<12} | {'ITEM NAME':<34} | {'PROFIT':>9} | {'ROI':>6} | {'V2 BLOCKER':<28}"
    print(m_header)
    print("-" * len(m_header))
    for m in sorted(missed, key=lambda x: -x["realized_pnl"])[:15]:
        print(
            f"{m['as_of_date']:<10} | {m['desk']:<9} | {m['supply_class']:<12} | {m['name'][:34]:<34} | "
            f"{m['realized_pnl']/1e6:>+7.1f}M | {m['roi_pct']:>5.1f}% | {m['blocker']:<28}"
        )


def main():
    parser = argparse.ArgumentParser(description="Point-in-time walk-forward replay & missed-winner auditor (v2).")
    parser.add_argument("--days-back", type=int, default=45, help="Total historical lookback window in days (default: 45)")
    parser.add_argument("--step-days", type=int, default=5, help="Days between simulated decision dates (default: 5)")
    parser.add_argument("--horizon-days", type=int, default=14, help="Forward execution horizon per decision date (default: 14)")
    parser.add_argument("--budget", type=float, default=50.0, help="Simulated portfolio budget in billions ISK (default: 50.0)")
    args = parser.parse_args()

    sde_meta_df = load_sde_item_metadata(SQLITE_PATH)
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        report = run_walkforward_replay(
            con=con,
            sde_meta_df=sde_meta_df,
            days_back=args.days_back,
            step_days=args.step_days,
            horizon_days=args.horizon_days,
            budget=args.budget * 1e9,
            sqlite_path=SQLITE_PATH,
            duckdb_path=DUCKDB_PATH,
        )
    finally:
        con.close()

    print_replay_report(report)


if __name__ == "__main__":
    main()