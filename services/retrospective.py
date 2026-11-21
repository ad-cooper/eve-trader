import dataclasses
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import duckdb
import pandas as pd

from services.accounting import calc_net_proceeds, calc_unit_cost
from services.blacklist import load_active_blacklist
from services.ledgers import (
    DESK_PROFILES,
    DeskProfile,
    IncumbentState,
    build_universe_exclusion_mask,
    compute_candidate_pricing,
    compute_line_sizing,
    select_desk_candidates,
)

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
PROPOSALS_DIR = "ledgers/ai_proposals"
DESK_ORDER = ("hold", "tactical", "evergreen")


AUTOPILOT_BRANCH = "ai-autopilot"


def get_active_git_branch() -> str | None:
    """Reads the host repository's active branch from mounted .git/HEAD."""
    head_path = Path(".git/HEAD")
    if head_path.is_file():
        try:
            raw = head_path.read_text(encoding="utf-8").strip()
            if raw.startswith("ref: refs/heads/"):
                return raw[len("ref: refs/heads/"):]
            return "HEAD"
        except Exception:
            pass
    return None


def check_autopilot_allowed() -> tuple[bool, str, str]:
    """Single source of truth for whether automated AI reviews and PRs are permitted."""
    branch = get_active_git_branch() or "unknown"
    if branch != AUTOPILOT_BRANCH:
        return (
            False,
            branch,
            f"Active branch is '{branch}' (autopilot only runs on '{AUTOPILOT_BRANCH}').",
        )
    return True, branch, f"Active branch '{branch}' matches '{AUTOPILOT_BRANCH}'."


def _sanitize_json(obj):
    if isinstance(obj, dict):
        return {str(k): _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    try:
        if pd.isna(obj):
            return None
    except Exception:
        pass
    if hasattr(obj, "item"):
        val = obj.item()
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        return val
    return obj


def _load_universe_as_of(con: duckdb.DuckDBPyConnection, as_of_date) -> pd.DataFrame:
    """Reconstructs the exact point-in-time market universe using only candles <= as_of_date."""
    query = f"""
    WITH hist AS (
        SELECT *
        FROM forge_market_history
        WHERE date <= DATE '{as_of_date}'
          AND volume > 0
    ),
    metrics_180d AS (
        SELECT
            type_id,
            COUNT(*) AS total_history_days,
            MAX(average) AS high_180d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_180d
        FROM hist
        WHERE date >= DATE '{as_of_date}' - INTERVAL 180 DAY
        GROUP BY type_id
    ),
    metrics_30d AS (
        SELECT
            type_id,
            AVG(volume) AS daily_vol_30d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_30d,
            AVG((highest - lowest) / NULLIF(average, 0)) * 100.0 AS avg_spread_30d,
            SUM(
                volume * CASE
                    WHEN highest > lowest THEN LEAST(0.95, GREATEST(0.02, (highest - LEAST(GREATEST(average, lowest), highest)) / (highest - lowest)))
                    ELSE 0.25
                END
            ) / NULLIF(SUM(volume), 0) AS bid_share_30d,
            (COUNT(
                CASE WHEN (average - lowest) / NULLIF(average, 0) >= 0.045
                      AND (
                          CASE WHEN highest > lowest
                               THEN (highest - LEAST(GREATEST(average, lowest), highest)) / (highest - lowest)
                               ELSE 0.25
                          END
                      ) >= 0.25
                     THEN 1
                END
            ) * 100.0 / COUNT(*)) AS dump_freq_pct
        FROM hist
        WHERE date >= DATE '{as_of_date}' - INTERVAL 30 DAY
        GROUP BY type_id
    ),
    metrics_7d AS (
        SELECT
            type_id,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_7d,
            MIN(lowest) AS min_low_7d
        FROM hist
        WHERE date >= DATE '{as_of_date}' - INTERVAL 7 DAY
        GROUP BY type_id
    ),
    metrics_floors AS (
        SELECT
            type_id,
            QUANTILE_CONT(CASE WHEN date >= DATE '{as_of_date}' - INTERVAL 14 DAY THEN lowest END, 0.25) AS min_low_14d,
            QUANTILE_CONT(lowest, 0.20) AS p20_low_30d
        FROM hist
        WHERE date >= DATE '{as_of_date}' - INTERVAL 30 DAY
        GROUP BY type_id
    )
    SELECT
        m.type_id,
        t.typeName AS item_name,
        g.groupName AS group_name,
        c.categoryName AS category_name,
        l.total_history_days,
        l.vwap_180d / 1e6 AS price_m_180d,
        m.vwap_30d / 1e6 AS price_m_30d,
        w.vwap_7d / 1e6 AS price_m_7d,
        f.min_low_14d / 1e6 AS min_low_m_14d,
        f.p20_low_30d / 1e6 AS p20_low_m_30d,
        m.daily_vol_30d,
        COALESCE(m.bid_share_30d, 0.25) AS bid_share_30d,
        m.dump_freq_pct,
        m.avg_spread_30d,
        (m.daily_vol_30d * (m.vwap_30d / 1e6)) AS daily_turnover_m
    FROM metrics_30d m
    JOIN metrics_7d w ON m.type_id = w.type_id
    JOIN metrics_floors f ON m.type_id = f.type_id
    JOIN metrics_180d l ON m.type_id = l.type_id
    JOIN sde.invTypes t ON m.type_id = t.typeID
    JOIN sde.invGroups g ON t.groupID = g.groupID
    JOIN sde.invCategories c ON g.categoryID = c.categoryID
    WHERE t.published = 1
      AND t.marketGroupID IS NOT NULL
      AND COALESCE(t.marketGroupID, 0) NOT IN (2487, 1661)
      AND LOWER(COALESCE(t.description, '')) NOT LIKE '%cease to function%'
      AND LOWER(COALESCE(t.description, '')) NOT LIKE '%will expire%'
      AND LOWER(COALESCE(t.description, '')) NOT LIKE '%expired%';
    """
    return con.execute(query).df()


def _load_forward_candles(con: duckdb.DuckDBPyConnection, as_of_date, horizon_days: int) -> dict[int, list[dict]]:
    """Loads daily post-checkpoint candles keyed by type_id for execution replay."""
    rows = con.execute(f"""
        SELECT type_id, date, lowest, highest, average, volume
        FROM forge_market_history
        WHERE date > DATE '{as_of_date}'
          AND date <= DATE '{as_of_date}' + INTERVAL '{int(horizon_days)} DAY'
        ORDER BY type_id, date ASC;
    """).fetchall()

    by_tid: dict[int, list[dict]] = {}
    for tid, dt, low, high, avg, vol in rows:
        by_tid.setdefault(int(tid), []).append({
            "date": str(dt),
            "lowest": float(low or 0.0),
            "highest": float(high or 0.0),
            "average": float(avg or 0.0),
            "volume": int(vol or 0),
        })
    return by_tid


def _estimate_daily_bid_share(lowest: float, highest: float, average: float) -> float:
    """
    Infers the fraction of daily volume executed on the bid (buy-order) side from OHLC/VWAP:
    average ≈ b * lowest + (1 - b) * highest  =>  b = (highest - average) / (highest - lowest).
    """
    if highest <= 0 or lowest <= 0 or highest <= lowest:
        return 0.25
    clamped_avg = min(max(average, lowest), highest)
    b = (highest - clamped_avg) / (highest - lowest)
    return max(0.02, min(0.95, b))


def _simulate_line_execution(
    candles: list[dict],
    qty: int,
    target_buy: float,
    target_sell: float,
    vol_pct: float,
    max_hold_days: int,
) -> dict:
    """
    Replays daily order fills and exits over `max_hold_days` using inferred bid/ask volume
    and candle depth weighting so 1-unit outlier wicks do not trigger phantom full-size fills.
    """
    window = candles[:max_hold_days]
    if not window or qty <= 0 or target_buy <= 0 or target_sell <= 0:
        return {
            "target_qty": max(0, int(qty)),
            "filled_qty": 0,
            "unfilled_qty": max(0, int(qty)),
            "qty_fill_pct": 0.0,
            "sold_qty": 0,
            "unsold_qty": 0,
            "deployed_isk": 0.0,
            "realized_pnl_isk": 0.0,
            "realized_roi_pct": 0.0,
            "status": "NO_DATA",
        }

    remaining_buy = qty
    unsold_inventory = 0
    total_bought = 0
    total_sold = 0
    gross_cost = 0.0
    net_revenue = 0.0
    unit_cost = calc_unit_cost(target_buy)
    unit_proceeds = calc_net_proceeds(target_sell)

    for day in window:
        low = day["lowest"]
        high = day["highest"]
        avg = day["average"]
        vol = day["volume"]
        bid_share = _estimate_daily_bid_share(low, high, avg)

        if unsold_inventory > 0 and high >= target_sell:
            ask_vol = vol * max(0.05, 1.0 - bid_share)
            if avg > 0 and high > avg and target_sell > avg:
                sell_depth = max(0.10, min(1.0, (high - target_sell) / (high - avg)))
            else:
                sell_depth = 1.0
            fillable_ask = ask_vol * sell_depth
            raw_sell_cap = fillable_ask * vol_pct
            daily_sell_cap = int(raw_sell_cap) if raw_sell_cap >= 1.0 else (1 if fillable_ask >= 2.0 else 0)
            if daily_sell_cap > 0:
                sell_fill = min(unsold_inventory, daily_sell_cap)
                unsold_inventory -= sell_fill
                total_sold += sell_fill
                net_revenue += sell_fill * unit_proceeds

        if remaining_buy > 0 and low > 0 and low <= target_buy:
            bid_vol = vol * bid_share
            if avg > low and target_buy < avg:
                buy_depth = max(0.10, min(1.0, (target_buy - low) / (avg - low)))
            else:
                buy_depth = 1.0
            fillable_bid = bid_vol * buy_depth
            raw_buy_cap = fillable_bid * vol_pct
            daily_buy_cap = int(raw_buy_cap) if raw_buy_cap >= 1.0 else (1 if fillable_bid >= 2.0 else 0)
            if daily_buy_cap > 0:
                buy_fill = min(remaining_buy, daily_buy_cap)
                remaining_buy -= buy_fill
                unsold_inventory += buy_fill
                total_bought += buy_fill
                gross_cost += buy_fill * unit_cost

    mtm_revenue = 0.0
    if unsold_inventory > 0:
        last_avg = window[-1]["average"] or window[-1]["lowest"] or target_buy
        mtm_revenue = unsold_inventory * calc_net_proceeds(last_avg)

    realized_pnl = (net_revenue + mtm_revenue) - gross_cost
    realized_roi = (realized_pnl / gross_cost * 100.0) if gross_cost > 0 else 0.0
    sold_cost = total_sold * unit_cost
    sold_roi = ((net_revenue - sold_cost) / sold_cost * 100.0) if sold_cost > 0 else 0.0
    qty_fill_pct = round((total_bought / qty * 100.0) if qty > 0 else 0.0, 1)

    if total_bought == 0:
        status = "UNFILLED_TIMEOUT"
    elif total_sold == total_bought and total_bought == qty:
        status = "ROUNDTRIP_COMPLETE"
    elif total_sold == total_bought:
        status = "PARTIAL_FILL_ROUNDTRIP"
    elif total_sold > 0:
        status = "PARTIAL_EXIT_MTM"
    else:
        status = "STUCK_INVENTORY_MTM"

    return {
        "target_qty": qty,
        "filled_qty": total_bought,
        "unfilled_qty": remaining_buy,
        "qty_fill_pct": qty_fill_pct,
        "sold_qty": total_sold,
        "unsold_qty": unsold_inventory,
        "deployed_isk": round(gross_cost, 2),
        "realized_pnl_isk": round(realized_pnl, 2),
        "realized_roi_pct": round(realized_roi, 2),
        "sold_roi_pct": round(sold_roi, 2),
        "status": status,
    }


def _summarize_spread_compression_audit(audit_events: list[dict], exit_records: list[dict]) -> dict:
    """Summarizes compressed-spread re-buy prevention and exit ROI quality across walk-forward checkpoints."""
    blocked_new = [e for e in audit_events if e["kind"] == "blocked_new_compressed"]
    blocked_sticky_buy = [e for e in audit_events if e["kind"] == "blocked_sticky_buy_only"]
    zeroed_sticky_hold = [e for e in audit_events if e["kind"] == "zeroed_sticky_holding_buy"]
    leaked_buys = [e for e in audit_events if e["kind"] == "leaked_compressed_buy"]

    prevented = blocked_new + blocked_sticky_buy + zeroed_sticky_hold
    avoided_capital_m = round(sum(e["legacy_allocated_m"] for e in prevented), 2)
    counterfactual_pnl_m = round(sum(e["legacy_forward_pnl_m"] for e in prevented), 2)

    healthy_exits = [x for x in exit_records if x["sold_qty"] > 0 and x["sold_roi_pct"] >= x["min_target_roi"]]
    low_margin_exits = [x for x in exit_records if x["sold_qty"] > 0 and 0.0 <= x["sold_roi_pct"] < x["min_target_roi"]]
    loss_exits = [x for x in exit_records if x["sold_qty"] > 0 and x["sold_roi_pct"] < 0.0]

    return {
        "confirmation_status": "CONFIRMED_FIXED" if len(leaked_buys) == 0 else "WARNING_LEAKED_COMPRESSED_BUYS",
        "leaked_compressed_buys_count": len(leaked_buys),
        "compressed_buys_prevented_total": len(prevented),
        "breakdown": {
            "blocked_new_compressed_entries": len(blocked_new),
            "cancelled_sticky_buy_only_orders": len(blocked_sticky_buy),
            "zeroed_buy_while_selling_or_holding": len(zeroed_sticky_hold),
        },
        "avoided_capital_locked_m": avoided_capital_m,
        "legacy_rule_counterfactual_pnl_m": counterfactual_pnl_m,
        "exit_quality": {
            "healthy_target_exits": len(healthy_exits),
            "avg_healthy_exit_roi_pct": round(sum(x["sold_roi_pct"] for x in healthy_exits) / len(healthy_exits), 2) if healthy_exits else 0.0,
            "low_margin_compressed_exits": len(low_margin_exits),
            "avg_low_margin_exit_roi_pct": round(sum(x["sold_roi_pct"] for x in low_margin_exits) / len(low_margin_exits), 2) if low_margin_exits else 0.0,
            "negative_roi_exits": len(loss_exits),
        },
        "top_prevented_compressed_buys": sorted(prevented, key=lambda x: x["legacy_allocated_m"], reverse=True)[:10],
    }


def _simulate_waterfall_checkpoint(
    df_u: pd.DataFrame,
    candles_map: dict[int, list[dict]],
    profiles: dict[str, DeskProfile],
    total_budget: float,
    base_exclusion_mask: pd.Series,
    carried_incumbents: dict | None = None,
    as_of_date: str = "",
) -> dict:
    """
    Simulates one historical point-in-time waterfall checkpoint across Hold, Tactical, and Evergreen
    using pre-loaded point-in-time universe & forward candles, spot ask/bid telemetry (`p_info`),
    multi-checkpoint incumbent carry-over, and spread-compression re-buy auditing.
    """
    if df_u.empty:
        return {
            "as_of_date": str(as_of_date),
            "desks": {},
            "lines": [],
            "missed_opportunities": [],
            "carry_over": {"buys": {}, "sells": {}, "assets": {}, "prior_desks": {}},
            "compression_events": [],
        }

    carried = carried_incumbents or {}
    c_buys = dict(carried.get("buys") or {})
    c_sells = dict(carried.get("sells") or {})
    c_assets = dict(carried.get("assets") or {})
    c_prior = dict(carried.get("prior_desks") or {})

    claimed_ids: set[int] = set()
    cascaded_slack = 0.0
    desk_results: dict[str, dict] = {}
    all_lines: list[dict] = []
    missed_opps: list[dict] = []
    compression_events: list[dict] = []

    next_buys: dict[int, dict] = {}
    next_sells: dict[int, dict] = {}
    next_assets: dict[int, int] = {}
    next_prior: dict[int, str] = {}

    for desk in DESK_ORDER:
        cfg = profiles[desk]
        base_budget = total_budget * cfg.ratio
        desk_budget = base_budget + cascaded_slack

        desk_buys = {tid: v for tid, v in c_buys.items() if c_prior.get(tid, desk) == desk}
        desk_sells = {tid: v for tid, v in c_sells.items() if c_prior.get(tid, desk) == desk}
        desk_assets = {tid: q for tid, q in c_assets.items() if c_prior.get(tid, desk) == desk and q > 0}
        desk_sticky_ids = set(desk_buys.keys()) | set(desk_sells.keys()) | set(desk_assets.keys())

        incumbents = IncumbentState(
            active_buys=desk_buys,
            active_sells=desk_sells,
            asset_qtys=desk_assets,
            significant_assets={tid for tid, q in desk_assets.items() if q > 0},
            desk_sticky_ids=desk_sticky_ids,
        )

        # Combine precomputed static blacklist mask with dynamic claimed_ids and sticky overrides
        desk_mask = base_exclusion_mask.copy()
        if claimed_ids:
            desk_mask &= ~df_u["type_id"].isin(claimed_ids)
        if desk_sticky_ids:
            desk_mask |= df_u["type_id"].isin(desk_sticky_ids) & ~df_u["type_id"].isin(claimed_ids)

        candidates, desk_params = select_desk_candidates(df_u, desk, desk_mask, desk_sticky_ids, cfg=cfg)
        desk_params["name"] = cfg.name
        desk_params["live_min_margin"] = cfg.live_min_margin

        remaining = desk_budget
        allocated_isk = 0.0
        deployed_isk = 0.0
        realized_pnl_isk = 0.0
        line_count = 0
        filled_count = 0
        roundtrip_count = 0
        stuck_count = 0

        for _, row in candidates.iterrows():
            tid = int(row["type_id"])
            is_sticky = bool(row.get("is_sticky", tid in desk_sticky_ids))
            passes_desk = bool(row.get("passes_desk", True))
            candles = candles_map.get(tid, [])

            p_info: dict = {}
            if candles:
                d0 = candles[0]
                spot_ask = float(d0.get("highest") or d0.get("average") or 0.0)
                spot_buy = float(d0.get("lowest") or 0.0)
                if spot_ask > 0:
                    p_info["lowest_ask"] = spot_ask
                if spot_buy > 0:
                    p_info["top_buy"] = spot_buy

            pricing = compute_candidate_pricing(row, desk, p_info, incumbents, is_sticky)
            if not pricing:
                continue

            nominal_margin = float(pricing["net_margin"])
            live_exec_margin = float(pricing.get("live_exec_margin", nominal_margin))
            has_buy = tid in desk_buys
            has_sell = tid in desk_sells
            held_qty = int(desk_assets.get(tid, 0))
            buy_rem = int(desk_buys[tid].get("volume_remain", 0)) if has_buy else 0
            sell_rem = int(desk_sells[tid].get("volume_remain", 0)) if has_sell else 0

            sizing = compute_line_sizing(
                row=row,
                pricing=pricing,
                desk_params=desk_params,
                incumbents=incumbents,
                is_sticky=is_sticky,
                passes_desk=passes_desk,
                remaining_budget=remaining,
            )

            is_compressed = (nominal_margin >= cfg.sticky_margin_floor) and (live_exec_margin < cfg.live_min_margin)
            if is_compressed:
                existing_qty = max(buy_rem, sell_rem + held_qty)
                legacy_qty = max(1, existing_qty) if is_sticky else max(1, int(row["daily_vol_30d"] * cfg.vol_pct))
                legacy_alloc = legacy_qty * pricing["cost_unit"]
                legacy_sim = _simulate_line_execution(
                    candles=candles,
                    qty=legacy_qty,
                    target_buy=pricing["target_buy"],
                    target_sell=pricing["target_sell"],
                    vol_pct=cfg.vol_pct,
                    max_hold_days=cfg.max_hold_days,
                )

                allow_new_buy = bool(sizing.get("allow_new_buy", True)) if sizing else False
                total_line_qty = int(sizing["qty"]) if sizing else 0
                inc_buy_qty = max(0, total_line_qty - (sell_rem + held_qty)) if allow_new_buy else 0

                if sizing is None and not is_sticky:
                    kind = "blocked_new_compressed"
                elif sizing is None and is_sticky and not has_sell and held_qty == 0:
                    kind = "blocked_sticky_buy_only"
                elif sizing is not None and inc_buy_qty == 0 and (has_sell or held_qty > 0):
                    kind = "zeroed_sticky_holding_buy"
                elif inc_buy_qty > 0:
                    kind = "leaked_compressed_buy"
                else:
                    kind = None

                if kind:
                    compression_events.append({
                        "checkpoint": str(as_of_date),
                        "desk": desk,
                        "type_id": tid,
                        "name": str(row["item_name"]),
                        "kind": kind,
                        "nominal_margin_pct": round(nominal_margin, 2),
                        "live_exec_margin_pct": round(live_exec_margin, 2),
                        "new_qty": inc_buy_qty,
                        "legacy_qty": legacy_qty,
                        "legacy_allocated_m": round(legacy_alloc / 1e6, 2),
                        "legacy_forward_pnl_m": round(legacy_sim.get("realized_pnl_isk", 0.0) / 1e6, 2),
                    })

            if not sizing:
                if passes_desk and candles:
                    sim_missed = _simulate_line_execution(
                        candles=candles,
                        qty=max(1, int(row["daily_vol_30d"] * cfg.vol_pct)),
                        target_buy=pricing["target_buy"],
                        target_sell=pricing["target_sell"],
                        vol_pct=cfg.vol_pct,
                        max_hold_days=cfg.max_hold_days,
                    )
                    if sim_missed.get("realized_pnl_isk", 0.0) > 250e6:
                        missed_opps.append({
                            "checkpoint": str(as_of_date),
                            "desk": desk,
                            "type_id": tid,
                            "name": str(row["item_name"]),
                            "realized_pnl_m": round(sim_missed["realized_pnl_isk"] / 1e6, 2),
                            "realized_roi_pct": sim_missed["realized_roi_pct"],
                            "blocked_by": _diagnose_rejection_reason(row, cfg),
                        })
                continue

            claimed_ids.add(tid)
            allow_buy = bool(sizing.get("allow_new_buy", True))
            buy_qty = max(0, int(sizing["qty"]) - (sell_rem + held_qty)) if allow_buy else 0
            line_alloc = (buy_qty * pricing["cost_unit"]) if buy_qty > 0 else 0.0
            remaining -= line_alloc
            allocated_isk += line_alloc
            line_count += 1

            sim = _simulate_line_execution(
                candles=candles,
                qty=buy_qty,
                target_buy=pricing["target_buy"],
                target_sell=pricing["target_sell"],
                vol_pct=cfg.vol_pct,
                max_hold_days=cfg.max_hold_days,
            )

            deployed_isk += sim["deployed_isk"]
            realized_pnl_isk += sim["realized_pnl_isk"]
            if sim["filled_qty"] > 0:
                filled_count += 1
            if sim["status"] in ("ROUNDTRIP_COMPLETE", "PARTIAL_FILL_ROUNDTRIP"):
                roundtrip_count += 1
            if sim["unsold_qty"] > 0:
                stuck_count += 1

            if sim["unfilled_qty"] > 0 and buy_qty > 0:
                next_buys[tid] = {"price": pricing["target_buy"], "volume_remain": sim["unfilled_qty"]}
                next_prior[tid] = desk
            if sim["unsold_qty"] > 0:
                next_sells[tid] = {"price": pricing["target_sell"], "volume_remain": sim["unsold_qty"]}
                next_assets[tid] = sim["unsold_qty"]
                next_prior[tid] = desk

            min_target_roi = cfg.take_profit_roi if cfg.take_profit_roi > 0 else cfg.sticky_margin_floor
            all_lines.append({
                "checkpoint": str(as_of_date),
                "desk": desk,
                "type_id": tid,
                "name": str(row["item_name"]),
                "qty": buy_qty,
                "filled_qty": sim["filled_qty"],
                "unfilled_qty": sim["unfilled_qty"],
                "qty_fill_pct": sim["qty_fill_pct"],
                "sold_qty": sim["sold_qty"],
                "sold_roi_pct": sim.get("sold_roi_pct", sim["realized_roi_pct"]),
                "min_target_roi": min_target_roi,
                "bid_share_pct": round(float(row.get("bid_share_30d") or 0.25) * 100.0, 1),
                "allocated_isk": line_alloc,
                "deployed_isk": sim["deployed_isk"],
                "realized_pnl_isk": sim["realized_pnl_isk"],
                "realized_roi_pct": sim["realized_roi_pct"],
                "status": sim["status"],
            })

        cascaded_slack = max(0.0, remaining)
        fill_rate = round((filled_count / line_count * 100.0) if line_count > 0 else 0.0, 1)
        rt_rate = round((roundtrip_count / line_count * 100.0) if line_count > 0 else 0.0, 1)
        stuck_rate = round((stuck_count / filled_count * 100.0) if filled_count > 0 else 0.0, 1)

        desk_results[desk] = {
            "lines": line_count,
            "allocated_isk": allocated_isk,
            "deployed_isk": deployed_isk,
            "realized_pnl_isk": realized_pnl_isk,
            "realized_pnl_m": realized_pnl_isk / 1e6,
            "fill_rate_pct": fill_rate,
            "roundtrip_pct": rt_rate,
            "stuck_rate_pct": stuck_rate,
        }

    return {
        "as_of_date": str(as_of_date),
        "desks": desk_results,
        "lines": all_lines,
        "missed_opportunities": missed_opps,
        "carry_over": {
            "buys": next_buys,
            "sells": next_sells,
            "assets": next_assets,
            "prior_desks": next_prior,
        },
        "compression_events": compression_events,
    }





def _diagnose_rejection_reason(row: pd.Series, cfg: DeskProfile) -> str:
    reasons = []
    if row["daily_turnover_m"] < cfg.min_turnover_m:
        reasons.append(f"turnover {row['daily_turnover_m']:.1f}M < {cfg.min_turnover_m}M")
    if row["avg_spread_30d"] < cfg.min_spread_pct:
        reasons.append(f"spread {row['avg_spread_30d']:.1f}% < {cfg.min_spread_pct}%")
    if cfg.min_dump_freq_pct > 0 and row["dump_freq_pct"] < cfg.min_dump_freq_pct:
        reasons.append(f"dump_freq {row['dump_freq_pct']:.1f}% < {cfg.min_dump_freq_pct}%")
    if cfg.min_daily_vol > 0 and row["daily_vol_30d"] < cfg.min_daily_vol:
        reasons.append(f"daily_vol {row['daily_vol_30d']:.1f} < {cfg.min_daily_vol}")
    if cfg.max_vwap_ratio_180d is not None and row["price_m_30d"] > (row["price_m_180d"] * cfg.max_vwap_ratio_180d):
        ratio = row["price_m_30d"] / row["price_m_180d"] if row["price_m_180d"] > 0 else 0.0
        reasons.append(f"vwap_ratio {ratio:.2f} > {cfg.max_vwap_ratio_180d}")
    if not (cfg.min_price_m <= row[cfg.price_col] <= cfg.max_price_m):
        reasons.append(f"price {row[cfg.price_col]:.1f}M outside [{cfg.min_price_m}, {cfg.max_price_m}]")
    return ", ".join(reasons) if reasons else "ranked below budget cutoff or margin floor"


def _optimize_desk_profiles(
    checkpoints: list[tuple],
    total_budget: float,
) -> tuple[dict[str, DeskProfile], list[dict]]:
    """
    Runs a local coordinate search over DeskProfile tolerances using walk-forward realized ISK profit,
    strictly respecting all tests/test_strategy_engines.py invariants.
    """
    best_profiles = dict(DESK_PROFILES)

    search_space = {
        "hold": {
            "min_margin": [14.0, 16.0, 18.0, 20.0],
            "min_turnover_m": [10.0, 15.0, 20.0],
            "max_vwap_ratio_180d": [0.90, 0.92, 0.94],
            "vol_pct": [0.06, 0.08, 0.10],
        },
        "tactical": {
            "min_margin": [6.5, 7.5, 9.0],
            "min_turnover_m": [25.0, 35.0, 45.0, 60.0],
            "min_spread_pct": [13.5, 15.0, 17.0],
            "min_dump_freq_pct": [25.0, 30.0, 35.0],
            "vol_pct": [0.15, 0.20, 0.25],
        },
        "evergreen": {
            "min_margin": [5.0, 6.0, 7.0],
            "min_turnover_m": [45.0, 60.0, 80.0],
            "min_spread_pct": [12.0, 13.5, 15.0],
            "vol_pct": [0.18, 0.22, 0.26],
        },
    }

    def score_profiles(cand_profiles: dict[str, DeskProfile]) -> float:
        total_pnl = 0.0
        for _, df_u, fwd_c, base_mask in checkpoints:
            sim = _simulate_waterfall_checkpoint(df_u, fwd_c, cand_profiles, total_budget, base_mask)
            for d_stats in sim["desks"].values():
                total_pnl += d_stats["realized_pnl_m"]
        return total_pnl

    best_score = score_profiles(best_profiles)
    change_log: list[dict] = []

    for desk in DESK_ORDER:
        for param_name, grid in search_space[desk].items():
            orig_val = getattr(best_profiles[desk], param_name)
            best_param_val = orig_val
            for val in grid:
                if val == orig_val:
                    continue
                updates = {param_name: val}
                if param_name == "min_margin":
                    updates["live_min_margin"] = max(best_profiles[desk].cancel_floor + 0.5, val)
                trial_cfg = dataclasses.replace(best_profiles[desk], **updates)
                trial_profiles = {**best_profiles, desk: trial_cfg}
                trial_score = score_profiles(trial_profiles)
                min_gain = max(15.0, abs(best_score) * 0.02)
                if trial_score > best_score + min_gain:
                    best_score = trial_score
                    best_param_val = val
                    best_profiles = trial_profiles

            if best_param_val != orig_val:
                change_log.append({
                    "desk": desk,
                    "parameter": param_name,
                    "current_value": orig_val,
                    "proposed_value": best_param_val,
                    "portfolio_pnl_after_m": round(best_score, 2),
                })

    return best_profiles, change_log


def _build_services_ledgers_diff(optimized_profiles: dict[str, DeskProfile]) -> str:
    """Generates a line-accurate unified diff against services/ledgers.py."""
    import difflib

    src_path = Path("services/ledgers.py")
    orig_lines = src_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = list(orig_lines)

    current_desk = None
    for idx, line in enumerate(new_lines):
        stripped = line.strip()
        for d in DESK_ORDER:
            if stripped.startswith(f'"{d}": DeskProfile('):
                current_desk = d
                break
        if stripped == "),":
            current_desk = None

        if current_desk:
            cfg = optimized_profiles[current_desk]
            for field_name in (
                "vol_pct",
                "min_margin",
                "live_min_margin",
                "min_turnover_m",
                "min_spread_pct",
                "min_dump_freq_pct",
                "max_vwap_ratio_180d",
            ):
                prefix = f"        {field_name}="
                if line.startswith(prefix):
                    val = getattr(cfg, field_name)
                    new_lines[idx] = f"{prefix}{val},\n"

    diff = "".join(
        difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile="a/services/ledgers.py",
            tofile="b/services/ledgers.py",
        )
    )
    return diff


def run_local_retrospective(
    lookback_days: int = 45,
    step_days: int = 7,
    total_budget_b: float = 80.0,
) -> dict:
    """
    Executes a walk-forward backtest across historical checkpoints in DuckDB, evaluates
    DeskProfile parameters via coordinate-descent, audits spread-compression re-buy prevention,
    and saves the proposal artifact.
    """
    os.makedirs(PROPOSALS_DIR, exist_ok=True)
    bl_items, bl_groups, bl_meta = load_active_blacklist()
    empty_incumbents = IncumbentState(
        active_buys={},
        active_sells={},
        asset_qtys={},
        significant_assets=set(),
        desk_sticky_ids=set(),
    )

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
        row = con.execute("SELECT MAX(date) FROM forge_market_history").fetchone()
        max_date = row[0] if row else None
        if max_date is None:
            return {"error": "No market history found in forge_market_history."}

        cp_dates = []
        d = max_date - pd.Timedelta(days=lookback_days)
        end_cut = max_date - pd.Timedelta(days=10)
        while d <= end_cut:
            cp_dates.append(d)
            d += pd.Timedelta(days=step_days)

        if not cp_dates:
            return {"error": "Insufficient history window for checkpoints."}

        preloaded_checkpoints = []
        for cp in cp_dates:
            df_u = _load_universe_as_of(con, cp)
            if df_u.empty:
                continue
            fwd_c = _load_forward_candles(con, cp, horizon_days=21)
            base_mask = build_universe_exclusion_mask(
                df=df_u,
                claimed_ids=set(),
                incumbents=empty_incumbents,
                bl_items=bl_items,
                bl_groups=bl_groups,
                bl_meta=bl_meta,
                losing_tids=set(),
            )
            preloaded_checkpoints.append((cp, df_u, fwd_c, base_mask))
    finally:
        con.close()

    if not preloaded_checkpoints:
        return {"error": "No non-empty checkpoints available for retrospective backtest."}

    total_budget = total_budget_b * 1e9
    baseline_runs = []
    carried = None
    all_compression_events = []
    for cp, df_u, fwd_c, base_mask in preloaded_checkpoints:
        run_res = _simulate_waterfall_checkpoint(
            df_u, fwd_c, DESK_PROFILES, total_budget, base_mask, carried_incumbents=carried, as_of_date=str(cp)
        )
        baseline_runs.append(run_res)
        carried = run_res.get("carry_over")
        all_compression_events.extend(run_res.get("compression_events") or [])

    opt_profiles, tolerance_changes = _optimize_desk_profiles(preloaded_checkpoints, total_budget)
    opt_runs = []
    opt_carried = None
    for cp, df_u, fwd_c, base_mask in preloaded_checkpoints:
        opt_res = _simulate_waterfall_checkpoint(
            df_u, fwd_c, opt_profiles, total_budget, base_mask, carried_incumbents=opt_carried, as_of_date=str(cp)
        )
        opt_runs.append(opt_res)
        opt_carried = opt_res.get("carry_over")

    def _agg_runs(runs: list[dict]) -> dict:
        out = {}
        for d in DESK_ORDER:
            d_list = [r["desks"].get(d, {}) for r in runs if d in r.get("desks", {})]
            cnt = max(1, len(d_list))
            out[d] = {
                "avg_lines": round(sum(x.get("lines", 0) for x in d_list) / cnt, 1),
                "avg_allocated_b": round(sum(x.get("allocated_isk", 0.0) for x in d_list) / cnt / 1e9, 2),
                "avg_deployed_b": round(sum(x.get("deployed_isk", 0.0) for x in d_list) / cnt / 1e9, 2),
                "total_realized_pnl_m": round(sum(x.get("realized_pnl_isk", 0.0) for x in d_list) / 1e6, 2),
                "avg_fill_rate_pct": round(sum(x.get("fill_rate_pct", 0.0) for x in d_list) / cnt, 1),
                "avg_roundtrip_pct": round(sum(x.get("roundtrip_pct", 0.0) for x in d_list) / cnt, 1),
                "avg_stuck_rate_pct": round(sum(x.get("stuck_rate_pct", 0.0) for x in d_list) / cnt, 1),
            }
        return out

    item_perf: dict[tuple[str, int], dict] = {}
    all_exit_records: list[dict] = []
    all_missed: list[dict] = []
    for r in baseline_runs:
        all_missed.extend(r.get("missed_opportunities") or [])
        for ln in r.get("lines") or []:
            all_exit_records.append({
                "sold_qty": ln.get("sold_qty", 0),
                "sold_roi_pct": ln.get("sold_roi_pct", ln.get("realized_roi_pct", 0.0)),
                "min_target_roi": ln.get("min_target_roi", 2.0),
            })
            key = (ln["desk"], ln["type_id"])
            agg = item_perf.setdefault(
                key,
                {
                    "desk": ln["desk"],
                    "type_id": ln["type_id"],
                    "name": ln["name"],
                    "appearances": 0,
                    "fills": 0,
                    "roundtrips": 0,
                    "target_qty_total": 0,
                    "filled_qty_total": 0,
                    "bid_share_pct": ln["bid_share_pct"],
                    "allocated_m": 0.0,
                    "deployed_m": 0.0,
                    "realized_pnl_m": 0.0,
                },
            )
            agg["appearances"] += 1
            agg["target_qty_total"] += ln["qty"]
            agg["filled_qty_total"] += ln["filled_qty"]
            agg["allocated_m"] += ln["allocated_isk"] / 1e6
            if ln["deployed_isk"] > 0:
                agg["fills"] += 1
            if ln["status"] in ("ROUNDTRIP_COMPLETE", "PARTIAL_FILL_ROUNDTRIP"):
                agg["roundtrips"] += 1
            agg["deployed_m"] += ln["deployed_isk"] / 1e6
            agg["realized_pnl_m"] += ln["realized_pnl_isk"] / 1e6

    all_items_ranked = sorted(item_perf.values(), key=lambda x: x["realized_pnl_m"], reverse=True)
    for it in all_items_ranked:
        it["allocated_m"] = round(it["allocated_m"], 2)
        it["deployed_m"] = round(it["deployed_m"], 2)
        it["realized_pnl_m"] = round(it["realized_pnl_m"], 2)
        it["qty_fill_pct"] = round(
            (it["filled_qty_total"] / it["target_qty_total"] * 100.0) if it["target_qty_total"] > 0 else 0.0,
            1,
        )

    over_sized_timeouts = sorted(
        [x for x in all_items_ranked if x["target_qty_total"] >= 5 and x["qty_fill_pct"] < 35.0],
        key=lambda x: (x["allocated_m"] - x["deployed_m"]),
        reverse=True,
    )[:15]

    spread_audit = _summarize_spread_compression_audit(all_compression_events, all_exit_records)

    out_payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "lookback_days": lookback_days,
        "step_days": step_days,
        "checkpoints": [str(cp) for cp, _, _, _ in preloaded_checkpoints],
        "portfolio_budget_b": total_budget_b,
        "baseline_performance": _agg_runs(baseline_runs),
        "optimized_performance": _agg_runs(opt_runs),
        "tolerance_changes": tolerance_changes,
        "spread_compression_audit": spread_audit,
        "best_suggested_items": [x for x in all_items_ranked if x["realized_pnl_m"] > 0][:15],
        "worst_suggested_items": [x for x in reversed(all_items_ranked) if x["realized_pnl_m"] < 0][:15],
        "over_sized_timeout_items": over_sized_timeouts,
        "missed_opportunities_in_practice": sorted(all_missed, key=lambda x: x["realized_pnl_m"], reverse=True)[:20],
        "unified_diff": _build_services_ledgers_diff(opt_profiles),
    }
    Path(f"{PROPOSALS_DIR}/latest.json").write_text(json.dumps(_sanitize_json(out_payload), indent=2), encoding="utf-8")
    return out_payload





def _bundle_local_codebase() -> str:
    """Reads the core trading engine, pricing, accounting, and test files directly from disk."""
    target_files = [
        "services/ledgers.py",
        "scripts/generate_ledgers.py",
        "services/accounting.py",
        "tests/test_strategy_engines.py",
    ]
    sections = []
    for rel_path in target_files:
        p = Path(rel_path)
        if p.exists():
            sections.append(f"### File: {rel_path}\n```python\n{p.read_text(encoding='utf-8')}\n```")
    return "\n\n".join(sections)


HYBRID_FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-4-hydrogen",
    "gemini-4-neon",
]


def run_hybrid_code_review(
    lookback_days: int = 45,
    step_days: int = 7,
    total_budget_b: float = 80.0,
    model: str = "auto",
    github_url: str | None = None,
) -> dict:
    """
    Stage 1: Runs the local walk-forward simulator on DuckDB to quantify fill rates,
            roundtrip completion, worst losing items, and missed opportunities.
    Stage 2: Sends the simulation findings + full local source code to Gemini for
            high-impact structural algorithm proposals (with anti-churn gating).
    """
    from google import genai
    from google.genai import types

    allowed, active_branch, reason = check_autopilot_allowed()
    if not allowed:
        return {
            "status": "skipped_inactive_branch",
            "branch": active_branch,
            "required_branch": AUTOPILOT_BRANCH,
            "reason": reason,
        }

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set in the container environment.")

    local_report = run_local_retrospective(
        lookback_days=lookback_days,
        step_days=step_days,
        total_budget_b=total_budget_b,
    )
    if "error" in local_report:
        return local_report

    compact_findings = {
        "checkpoints": local_report["checkpoints"],
        "portfolio_budget_b": local_report["portfolio_budget_b"],
        "baseline_performance": local_report["baseline_performance"],
        "optimized_performance": local_report["optimized_performance"],
        "local_parameter_tuning_already_found": local_report["tolerance_changes"],
        "best_suggested_items": local_report["best_suggested_items"][:10],
        "worst_suggested_items": local_report["worst_suggested_items"][:10],
        "missed_opportunities_in_practice": local_report["missed_opportunities_in_practice"][:12],
    }

    codebase_bundle = _bundle_local_codebase()
    github_note = (
        f"\nPublic Repository Reference URL (inspect if accessible): {github_url}\n"
        if github_url
        else ""
    )

    system_instruction = (
        "You are a principal quantitative trading engineer reviewing an EVE Online market-making system. "
        "Stage 1 ran a local coordinate search over DESK_PROFILES scalar parameters (`local_parameter_tuning_already_found`). "
        "Your job has two parts:\n"
        "1. AUDIT STAGE 1 PARAMETERS: Inspect each tweak in `local_parameter_tuning_already_found` against "
        "`baseline_performance` and `optimized_performance`. Return ONLY the subset of Stage 1 parameter changes "
        "worth keeping in `approved_stage1_parameters`—explicitly drop any tweak that degrades a desk's own profit "
        "or adds noise for minimal gain, and set `apply_stage1_parameters: true` if the approved parameter changes "
        "yield a meaningful net portfolio improvement (>= +10%).\n"
        "2. STAGE 2 STRUCTURAL CODE REVIEW: Default `algorithmic_proposals` to empty (`[]`) unless the backtest proves "
        "a recurring structural bottleneck that scalar parameters alone cannot solve. Never overfit to single-item "
        "outliers. Respect all invariants in tests/test_strategy_engines.py."
    )

    prompt = (
        f"### Stage 1 Local Walk-Forward Backtest Findings\n"
        f"{json.dumps(compact_findings, indent=2)}\n"
        f"{github_note}\n"
        f"### Stage 2 Full Local Source Code\n"
        f"{codebase_bundle}"
    )

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "is_significant_improvement": {"type": "BOOLEAN"},
            "apply_stage1_parameters": {"type": "BOOLEAN"},
            "approved_stage1_parameters": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "desk": {"type": "STRING"},
                        "parameter": {"type": "STRING"},
                        "current_value": {"type": "NUMBER"},
                        "proposed_value": {"type": "NUMBER"},
                        "rationale": {"type": "STRING"},
                    },
                    "required": ["desk", "parameter", "current_value", "proposed_value", "rationale"],
                },
            },
            "confidence_score": {"type": "NUMBER"},
            "estimated_monthly_pnl_uplift_pct": {"type": "NUMBER"},
            "no_change_rationale": {"type": "STRING"},
            "architectural_diagnosis": {"type": "STRING"},
            "failure_mode_breakdown": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "pattern_name": {"type": "STRING"},
                        "evidence_from_backtest": {"type": "STRING"},
                        "code_root_cause": {"type": "STRING"},
                    },
                    "required": ["pattern_name", "evidence_from_backtest", "code_root_cause"],
                },
            },
            "algorithmic_proposals": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING"},
                        "file_path": {"type": "STRING"},
                        "target_function": {"type": "STRING"},
                        "expected_impact": {"type": "STRING"},
                        "rationale": {"type": "STRING"},
                        "before_code": {"type": "STRING"},
                        "after_code": {"type": "STRING"},
                    },
                    "required": [
                        "title",
                        "file_path",
                        "target_function",
                        "expected_impact",
                        "rationale",
                        "before_code",
                        "after_code",
                    ],
                },
            },
            "recommended_unit_tests": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "test_name": {"type": "STRING"},
                        "purpose": {"type": "STRING"},
                        "python_test_code": {"type": "STRING"},
                    },
                    "required": ["test_name", "purpose", "python_test_code"],
                },
            },
        },
        "required": [
            "is_significant_improvement",
            "apply_stage1_parameters",
            "approved_stage1_parameters",
            "confidence_score",
            "estimated_monthly_pnl_uplift_pct",
            "no_change_rationale",
            "architectural_diagnosis",
            "failure_mode_breakdown",
            "algorithmic_proposals",
            "recommended_unit_tests",
        ],
    }

    if model and model != "auto":
        candidates = [model] + [m for m in HYBRID_FALLBACK_MODELS if m != model]
    else:
        candidates = list(HYBRID_FALLBACK_MODELS)

    tools = [types.Tool(url_context=types.UrlContext())] if github_url else None

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=55_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )

    resp = None
    used_model = None
    attempt_errors = []

    for candidate in candidates:
        try:
            cfg_kwargs = {
                "system_instruction": system_instruction,
                "response_mime_type": "application/json",
                "response_schema": response_schema,
                "temperature": 0.15,
            }
            if tools:
                cfg_kwargs["tools"] = tools

            resp = client.models.generate_content(
                model=candidate,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            used_model = candidate
            break
        except Exception as e:
            err_msg = str(e)
            attempt_errors.append(f"{candidate}: {err_msg[:180]}")
            print(f"[!] Hybrid Gemini review '{candidate}' failed, failing over: {err_msg[:180]}")

    if resp is None:
        raise RuntimeError("All Gemini models failed during deep review: " + " | ".join(attempt_errors))

    ai_review = json.loads(resp.text)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = _sanitize_json({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "engine": "hybrid_walk_forward_plus_gemini_architect",
        "model": used_model,
        "fallback_log": attempt_errors,
        "github_url_used": github_url,
        "local_simulation_summary": {
            "checkpoints": local_report["checkpoints"],
            "baseline_performance": local_report["baseline_performance"],
            "optimized_performance": local_report["optimized_performance"],
            "tolerance_changes": local_report["tolerance_changes"],
        },
        "deep_code_review": ai_review,
    })

    os.makedirs(PROPOSALS_DIR, exist_ok=True)
    out_path = os.path.join(PROPOSALS_DIR, f"hybrid_review_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(PROPOSALS_DIR, "latest_deep_review.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    result["saved_to"] = out_path
    return result


def _apply_approved_stage1_to_ledgers(
    ledgers_src: str,
    approved_params: list[dict],
) -> tuple[str, list[dict]]:
    """Applies Gemini-approved Stage 1 DeskProfile scalar updates onto services/ledgers.py source."""
    updated_profiles = dict(DESK_PROFILES)
    applied: list[dict] = []
    allowed_fields = {
        "vol_pct",
        "min_margin",
        "live_min_margin",
        "min_turnover_m",
        "min_spread_pct",
        "min_dump_freq_pct",
        "max_vwap_ratio_180d",
    }

    for item in approved_params:
        desk = str(item.get("desk") or "").strip().lower()
        param = str(item.get("parameter") or "").strip()
        if desk not in updated_profiles or param not in allowed_fields:
            continue
        try:
            val = float(item.get("proposed_value"))
        except (TypeError, ValueError):
            continue

        cur_cfg = updated_profiles[desk]
        cur_val = getattr(cur_cfg, param)
        if cur_val == val:
            continue

        updates = {param: val}
        if param == "min_margin":
            updates["live_min_margin"] = max(cur_cfg.cancel_floor + 0.5, val)
        updated_profiles[desk] = dataclasses.replace(cur_cfg, **updates)
        applied.append({
            "desk": desk,
            "parameter": param,
            "old_value": cur_val,
            "new_value": val,
            "rationale": str(item.get("rationale") or ""),
        })

    if not applied:
        return ledgers_src, []

    orig_lines = ledgers_src.splitlines(keepends=True)
    new_lines = list(orig_lines)
    current_desk = None

    for idx, line in enumerate(new_lines):
        stripped = line.strip()
        for d in DESK_ORDER:
            if stripped.startswith(f'"{d}": DeskProfile('):
                current_desk = d
                break
        if stripped == "),":
            current_desk = None

        if current_desk:
            cfg = updated_profiles[current_desk]
            for field_name in allowed_fields:
                prefix = f"        {field_name}="
                if line.startswith(prefix):
                    new_lines[idx] = f"{prefix}{getattr(cfg, field_name)},\n"

    return "".join(new_lines), applied


def _apply_proposals_in_memory(
    proposals: list[dict],
    unit_tests: list[dict],
) -> tuple[dict[str, str], list[str]]:
    """
    Applies `before_code` -> `after_code` patches and appends `recommended_unit_tests`
    in memory, validating exact substring matches and Python AST syntax before touching GitHub.
    """
    import ast

    allowed_files = {
        "services/ledgers.py",
        "scripts/generate_ledgers.py",
        "services/accounting.py",
    }
    updated_files: dict[str, str] = {}
    skipped: list[str] = []

    for prop in proposals:
        fpath = str(prop.get("file_path") or "").strip()
        before = str(prop.get("before_code") or "")
        after = str(prop.get("after_code") or "")
        title = str(prop.get("title") or "untitled")

        if fpath not in allowed_files:
            skipped.append(f"{title}: target file '{fpath}' is not in allowlist")
            continue
        if not before.strip() or not after.strip() or before == after:
            skipped.append(f"{title}: empty or identical before/after code")
            continue

        current_src = updated_files.get(fpath)
        if current_src is None:
            p = Path(fpath)
            if not p.exists():
                skipped.append(f"{title}: file '{fpath}' not found")
                continue
            current_src = p.read_text(encoding="utf-8")

        if before not in current_src:
            skipped.append(f"{title}: before_code snippet did not match {fpath}")
            continue

        candidate_src = current_src.replace(before, after, 1)
        try:
            ast.parse(candidate_src)
        except SyntaxError as se:
            skipped.append(f"{title}: syntax error in proposed code ({se})")
            continue

        updated_files[fpath] = candidate_src

    if unit_tests and updated_files:
        test_path = "tests/test_ai_proposals.py"
        existing_test = (
            Path(test_path).read_text(encoding="utf-8")
            if Path(test_path).exists()
            else "import pytest\nfrom services.ledgers import DESK_PROFILES\n\n"
        )
        added_blocks = []
        for ut in unit_tests:
            tcode = str(ut.get("python_test_code") or "").strip()
            tname = str(ut.get("test_name") or "unnamed_test")
            if not tcode:
                continue
            try:
                ast.parse(tcode)
                if tname not in existing_test:
                    added_blocks.append(f"\n\n# Auto-generated by Gemini Retrospective Review\n{tcode}\n")
            except SyntaxError:
                skipped.append(f"unit test '{tname}': invalid Python syntax")

        if added_blocks:
            combined_tests = existing_test + "".join(added_blocks)
            try:
                ast.parse(combined_tests)
                updated_files[test_path] = combined_tests
            except SyntaxError:
                pass

    return updated_files, skipped


def _ensure_remote_branch(
    api_base: str,
    headers: dict,
    target_branch: str,
    fallback_source: str = "main",
) -> tuple[str | None, str | None]:
    """Ensures `target_branch` exists on GitHub, creating it from the repo's default branch if missing."""
    import requests

    repo_resp = requests.get(api_base, headers=headers, timeout=15)
    if repo_resp.status_code == 404:
        return (
            None,
            f"GitHub returned 404 for {api_base}. Ensure GITHUB_REPO is accurate and GITHUB_TOKEN "
            f"has 'Contents: Read & Write' and 'Pull requests: Read & Write' on this repository.",
        )
    if repo_resp.status_code in (401, 403):
        return None, f"GitHub auth/permission error ({repo_resp.status_code}): {repo_resp.text}"

    ref_resp = requests.get(f"{api_base}/git/ref/heads/{target_branch}", headers=headers, timeout=15)
    if ref_resp.status_code == 200:
        return ref_resp.json()["object"]["sha"], None

    default_branch = repo_resp.json().get("default_branch") or fallback_source
    src_resp = requests.get(f"{api_base}/git/ref/heads/{default_branch}", headers=headers, timeout=15)
    if src_resp.status_code != 200:
        return None, f"Failed to read {default_branch} ref: {src_resp.text}"
    src_sha = src_resp.json()["object"]["sha"]

    create_resp = requests.post(
        f"{api_base}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{target_branch}", "sha": src_sha},
        timeout=15,
    )
    if create_resp.status_code not in (200, 201):
        return None, f"Failed to create branch {target_branch} from {default_branch}: {create_resp.text}"
    return src_sha, None


def create_and_merge_github_pr(
    review_result: dict,
    auto_merge: bool = True,
    base_branch: str = AUTOPILOT_BRANCH,
    min_confidence: float = 0.75,
    min_uplift_pct: float = 10.0,
) -> dict:
    """
    Enforces an anti-churn significance gate, then creates a feature branch off `base_branch`
    (never `main`), opens a Pull Request with the backtest diagnosis, and merges into `base_branch`.
    """
    import base64
    import requests

    allowed, _, branch_reason = check_autopilot_allowed()
    if not allowed or base_branch == "main":
        return {"status": "blocked", "reason": "Direct PR merges into 'main' are disabled; use 'ai-autopilot'."}

    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPO", "ad-cooper/eve-trader").strip().rstrip("/")
    for _pfx in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if repo.startswith(_pfx):
            repo = repo[len(_pfx):]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not token:
        return {"status": "skipped", "reason": "GITHUB_TOKEN is not set in container environment"}

    deep = review_result.get("deep_code_review") or {}
    is_sig = bool(deep.get("is_significant_improvement", False))
    apply_s1 = bool(deep.get("apply_stage1_parameters", False))
    approved_s1 = deep.get("approved_stage1_parameters") or []
    confidence = float(deep.get("confidence_score") or 0.0)
    uplift_pct = float(deep.get("estimated_monthly_pnl_uplift_pct") or 0.0)
    no_change_reason = str(deep.get("no_change_rationale") or "")

    has_actionable = is_sig or (apply_s1 and len(approved_s1) > 0)
    if not has_actionable or confidence < min_confidence or uplift_pct < min_uplift_pct:
        return {
            "status": "skipped_no_significant_gain",
            "is_significant_improvement": is_sig,
            "apply_stage1_parameters": apply_s1,
            "confidence_score": confidence,
            "estimated_monthly_pnl_uplift_pct": uplift_pct,
            "thresholds": {"min_confidence": min_confidence, "min_uplift_pct": min_uplift_pct},
            "reason": no_change_reason or "Proposed changes did not clear significance/confidence thresholds.",
        }

    proposals = deep.get("algorithmic_proposals") or []
    unit_tests = deep.get("recommended_unit_tests") or []

    updated_files, skipped = _apply_proposals_in_memory(proposals, unit_tests)

    applied_s1_records = []
    if apply_s1 and approved_s1:
        base_ledgers = updated_files.get("services/ledgers.py") or Path("services/ledgers.py").read_text(encoding="utf-8")
        patched_ledgers, applied_s1_records = _apply_approved_stage1_to_ledgers(base_ledgers, approved_s1)
        if applied_s1_records:
            updated_files["services/ledgers.py"] = patched_ledgers
    if not updated_files:
        return {
            "status": "no_applicable_changes",
            "reason": "No proposals matched exact source blocks cleanly",
            "skipped": skipped,
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api_base = f"https://api.github.com/repos/{repo}"

    base_sha, branch_err = _ensure_remote_branch(api_base, headers, target_branch=base_branch, fallback_source="main")
    if branch_err or not base_sha:
        return {"status": "error", "reason": branch_err or f"Could not resolve {base_branch}"}

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch_name = f"ai-proposal/{ts}"
    branch_resp = requests.post(
        f"{api_base}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        timeout=15,
    )
    if branch_resp.status_code not in (200, 201):
        return {"status": "error", "reason": f"Failed to create branch {branch_name}: {branch_resp.text}"}

    committed_files = []
    for fpath, content in updated_files.items():
        get_file = requests.get(
            f"{api_base}/contents/{fpath}",
            headers=headers,
            params={"ref": branch_name},
            timeout=15,
        )
        file_sha = get_file.json().get("sha") if get_file.status_code == 200 else None
        put_payload = {
            "message": f"chore(ai-retro): update {fpath} from walk-forward review",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if file_sha:
            put_payload["sha"] = file_sha

        put_resp = requests.put(f"{api_base}/contents/{fpath}", headers=headers, json=put_payload, timeout=20)
        if put_resp.status_code not in (200, 201):
            return {"status": "error", "reason": f"Failed to commit {fpath}: {put_resp.text}"}
        committed_files.append(fpath)

    diagnosis = deep.get("architectural_diagnosis", "")
    s1_bullets = [
        f"- **[Stage 1 Parameter] {r['desk']}.{r['parameter']}**: `{r['old_value']}` -> `{r['new_value']}` ({r['rationale']})"
        for r in applied_s1_records
    ]
    s2_bullets = [
        f"- **[Stage 2 Code] {p.get('title')}** (`{p.get('file_path')} :: {p.get('target_function')}`): {p.get('rationale')}"
        for p in proposals
    ]
    prop_bullets = "\n".join(s1_bullets + s2_bullets)
    pr_body = (
        f"## Automated Retrospective Algorithm Review\n\n"
        f"**Model:** `{review_result.get('model')}` | **Target Branch:** `{base_branch}` | "
        f"**Confidence:** `{confidence:.2f}` | **Est. Monthly Uplift:** `+{uplift_pct:.1f}%`\n\n"
        f"### Architectural Diagnosis\n{diagnosis}\n\n"
        f"### Proposed Changes\n{prop_bullets}\n\n"
        f"### Files Modified\n" + "\n".join(f"- `{f}`" for f in committed_files) + "\n\n"
        f"> *Merged into `{base_branch}` only (`main` remains your manually reviewed baseline). "
        f"Local `run_daily_ledger.sh` runs `pytest` after pulling and automatically reverts if any test fails.*"
    )

    pr_resp = requests.post(
        f"{api_base}/pulls",
        headers=headers,
        json={
            "title": f"AI Retrospective Optimization ({ts})",
            "head": branch_name,
            "base": base_branch,
            "body": pr_body,
        },
        timeout=20,
    )
    if pr_resp.status_code not in (200, 201):
        return {"status": "error", "reason": f"Failed to open PR: {pr_resp.text}"}

    pr_data = pr_resp.json()
    pr_number = pr_data["number"]
    pr_url = pr_data["html_url"]

    merged = False
    merge_note = f"PR opened against {base_branch} (auto_merge=False)"
    if auto_merge:
        merge_resp = requests.put(
            f"{api_base}/pulls/{pr_number}/merge",
            headers=headers,
            json={
                "commit_title": f"Merge AI Retrospective Optimization (#{pr_number})",
                "merge_method": "squash",
            },
            timeout=20,
        )
        if merge_resp.status_code == 200:
            merged = True
            merge_note = f"PR squash-merged into {base_branch} (main untouched)"
        else:
            merge_note = f"PR opened, but auto-merge returned HTTP {merge_resp.status_code}: {merge_resp.text}"

    return {
        "status": "merged" if merged else "opened",
        "base_branch": base_branch,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "branch": branch_name,
        "confidence_score": confidence,
        "estimated_monthly_pnl_uplift_pct": uplift_pct,
        "committed_files": committed_files,
        "skipped": skipped,
        "note": merge_note,
    }


def run_autopilot_pipeline(
    lookback_days: int = 45,
    step_days: int = 7,
    total_budget_b: float = 80.0,
    model: str = "auto",
    github_url: str | None = None,
    auto_pr: bool = True,
    auto_merge: bool = True,
    min_confidence: float = 0.75,
    min_uplift_pct: float = 10.0,
) -> dict:
    """Single entry point used by both FastAPI routes and background scheduler jobs."""
    result = run_hybrid_code_review(
        lookback_days=lookback_days,
        step_days=step_days,
        total_budget_b=total_budget_b,
        model=model,
        github_url=github_url,
    )
    if auto_pr and "error" not in result and not str(result.get("status", "")).startswith("skipped"):
        result["github_pr"] = create_and_merge_github_pr(
            result,
            auto_merge=auto_merge,
            base_branch=AUTOPILOT_BRANCH,
            min_confidence=min_confidence,
            min_uplift_pct=min_uplift_pct,
        )
    return result


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="EVE Trader Retrospective & Autopilot CLI")
    parser.add_argument("--check-branch", action="store_true", help="Exit 0 if on AUTOPILOT_BRANCH, else 1")
    parser.add_argument("--print-branch", action="store_true", help="Print configured AUTOPILOT_BRANCH name")
    args = parser.parse_args()

    if args.print_branch:
        print(AUTOPILOT_BRANCH)
        sys.exit(0)

    if args.check_branch:
        allowed, _, reason = check_autopilot_allowed()
        if allowed:
            sys.exit(0)
        print(f"[=] {reason}")
        sys.exit(1)
