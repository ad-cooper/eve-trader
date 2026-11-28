import dataclasses
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

from constants import (
    DESK_CONFIG_PATH,
    PROPOSALS_DIR,
    TUNABLE_PROFILE_FIELDS
)
from services.exclusions import load_active_blacklist
from services.desk_config import (
    DESK_PROFILES
)
from services.ledgers import (
    DeskProfile,
    IncumbentState,
)
from services.market_universe import build_universe_exclusion_mask
from services.analytics_db import open_analytics_connection
from services.retrospective.simulator import (
    DESK_ORDER,
    _load_forward_candles,
    _load_universe_as_of,
    _simulate_waterfall_checkpoint,
    _summarize_spread_compression_audit,
)

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

def _build_desk_config_diff(optimized_profiles: dict[str, DeskProfile]) -> str:
    """Generates a line-accurate unified diff against services/desk_config.py."""
    import difflib

    if not DESK_CONFIG_PATH.exists():
        return ""

    orig_lines = DESK_CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
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

        if current_desk and current_desk in optimized_profiles:
            cfg = optimized_profiles[current_desk]
            for field_name in TUNABLE_PROFILE_FIELDS:
                prefix = f"        {field_name}="
                if line.startswith(prefix):
                    val = getattr(cfg, field_name)
                    new_lines[idx] = f"{prefix}{val},\n"

    return "".join(
        difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile="a/services/desk_config.py",
            tofile="b/services/desk_config.py",
        )
    )

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

    with open_analytics_connection(read_only=True, attach_sde=True) as con:
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
