import duckdb
import math
import pandas as pd

from services.accounting import calc_net_proceeds, calc_unit_cost
from services.ledgers import (
    DeskProfile,
    IncumbentState,
    compute_candidate_pricing,
    compute_line_sizing,
    select_desk_candidates,
)

from constants import (
    DESK_ORDER
)

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
