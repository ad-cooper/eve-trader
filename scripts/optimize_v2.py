#!/usr/bin/env python3
"""
Walk-Forward AI Parameter Optimizer for Strategy v2
===================================================
Evaluates candidate parameter refinements on top of `v2` (SDE supply elasticity +
book-depth filtering) using zero-lookahead point-in-time walk-forward replay
(`scripts/backtest_replay.py`). Promotes `v2` (and winning overrides if they beat
baseline `v2` out-of-sample) to active production when `--promote` is passed.
"""

from __future__ import annotations
import os
import sys
import re
import json
import argparse
import dataclasses
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import services.ledgers as led_mod
from services.strategy_versions import (
    StrategyVersion,
    register_strategy_version,
    get_strategy_version,
    set_active_strategy_version,
)
from scripts.backtest_replay import (
    DUCKDB_PATH,
    SQLITE_PATH,
    load_sde_item_metadata,
    run_walkforward_replay,
)


def _register_tuned_v2_candidate(name: str, profile_overrides: dict[str, dict]) -> str:
    base_v2 = get_strategy_version("v2")

    def _with_overrides(fn):
        def wrapped(*args, **kwargs):
            saved = {k: dataclasses.replace(v) for k, v in led_mod.DESK_PROFILES.items()}
            try:
                for desk, updates in profile_overrides.items():
                    if desk in led_mod.DESK_PROFILES:
                        led_mod.DESK_PROFILES[desk] = dataclasses.replace(
                            led_mod.DESK_PROFILES[desk], **updates
                        )
                return fn(*args, **kwargs)
            finally:
                led_mod.DESK_PROFILES.clear()
                led_mod.DESK_PROFILES.update(saved)
        return wrapped

    register_strategy_version(
        StrategyVersion(
            version=name,
            description=f"V2 AI Candidate ({name}): {profile_overrides}",
            enrich_universe=base_v2.enrich_universe,
            select_candidates=_with_overrides(base_v2.select_candidates),
            price_candidate=_with_overrides(base_v2.price_candidate),
            size_line=_with_overrides(base_v2.size_line),
            evaluate_buy_order=base_v2.evaluate_buy_order,
            evaluate_sell_collision=base_v2.evaluate_sell_collision,
        )
    )
    return name


def summarize_version_metrics(df_trades: pd.DataFrame, version: str) -> dict:
    sub = df_trades[df_trades["strategy_version"] == version]
    filled = sub[sub["filled_qty"] > 0]
    deployed = float(filled["deployed_capital"].sum()) if not filled.empty else 0.0
    realized = float(filled["realized_pnl"].sum()) if not filled.empty else 0.0
    mtm = float(filled["mtm_pnl"].sum()) if not filled.empty else 0.0
    stops = int((sub["status"] == "TIME_STOP").sum()) if not sub.empty else 0
    stop_pct = (stops / max(1, len(filled))) * 100.0
    roi = (realized / deployed * 100.0) if deployed > 0 else 0.0
    score = realized * (1.0 - min(0.5, stop_pct / 200.0))
    return {
        "version": version,
        "orders": len(sub),
        "filled": len(filled),
        "deployed_b": round(deployed / 1e9, 2),
        "realized_b": round(realized / 1e9, 2),
        "mtm_b": round(mtm / 1e9, 2),
        "roi_pct": round(roi, 2),
        "stop_pct": round(stop_pct, 1),
        "score": score,
    }


def main():
    parser = argparse.ArgumentParser(description="Walk-forward AI optimizer & promoter for Strategy v2.")
    parser.add_argument("--days-back", type=int, default=45, help="Lookback window in days (default: 45)")
    parser.add_argument("--step-days", type=int, default=5, help="Step between replay dates (default: 5)")
    parser.add_argument("--horizon-days", type=int, default=14, help="Execution horizon per date (default: 14)")
    parser.add_argument("--budget", type=float, default=90.0, help="Portfolio budget in billions ISK (default: 90.0)")
    parser.add_argument("--promote", action="store_true", help="Promote winning v2 profile to active production")
    args = parser.parse_args()

    candidates = {
        "v2-tight-hold": {
            "hold": {"max_vwap_ratio_180d": 0.88, "min_margin": 22.0},
            "tactical": {"min_margin": 8.0},
        },
        "v2-fast-velocity": {
            "evergreen": {"min_margin": 6.5, "vol_pct": 0.12},
            "tactical": {"vol_pct": 0.16, "min_margin": 7.5},
        },
        "v2-balanced-alpha": {
            "hold": {"max_vwap_ratio_180d": 0.89, "min_margin": 21.0},
            "tactical": {"min_margin": 8.0, "vol_pct": 0.16},
            "evergreen": {"min_margin": 6.5},
        },
    }

    versions_to_eval = ["v1", "v2"]
    for cname, overrides in candidates.items():
        versions_to_eval.append(_register_tuned_v2_candidate(cname, overrides))

    sde_meta_df = load_sde_item_metadata(SQLITE_PATH)
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        report = run_walkforward_replay(
            con=con,
            sde_meta_df=sde_meta_df,
            versions=versions_to_eval,
            days_back=args.days_back,
            step_days=args.step_days,
            horizon_days=args.horizon_days,
            budget=args.budget * 1e9,
            sqlite_path=SQLITE_PATH,
            duckdb_path=DUCKDB_PATH,
        )
    finally:
        con.close()

    results = [summarize_version_metrics(report["trades"], v) for v in versions_to_eval]

    print("\n" + "=" * 96)
    print(f"V2 AI WALK-FORWARD OPTIMIZATION LEADERBOARD ({args.days_back}d Replay @ {args.budget:.1f}B ISK)")
    print("=" * 96)
    print(f"{'VERSION':<18} | {'FILLED':>8} | {'DEPLOYED':>10} | {'REALIZED PNL':>12} | {'ROI %':>8} | {'STOP %':>7} | {'SCORE':>10}")
    print("-" * 96)
    for r in sorted(results, key=lambda x: -x["score"]):
        print(
            f"{r['version']:<18} | {r['filled']:>4}/{r['orders']:<3} | {r['deployed_b']:>8.2f}B | "
            f"{r['realized_b']:>+10.2f}B | {r['roi_pct']:>7.2f}% | {r['stop_pct']:>6.1f}% | {r['score']/1e9:>+8.2f}B"
        )

    by_ver = {r["version"]: r for r in results}
    v2_base = by_ver["v2"]
    best_cand = max([by_ver[c] for c in candidates], key=lambda x: x["score"])

    winner_name = "v2"
    winning_overrides: dict[str, dict] = {}
    if best_cand["score"] > v2_base["score"] * 1.03 and best_cand["realized_b"] > v2_base["realized_b"]:
        winner_name = best_cand["version"]
        winning_overrides = candidates[winner_name]
        print(f"\n[+] AI Candidate '{winner_name}' beat baseline v2 (+{best_cand['realized_b'] - v2_base['realized_b']:.2f}B realized P&L)!")
    else:
        print(f"\n[*] Baseline 'v2' outperformed or matched all parameter perturbations ({v2_base['realized_b']:+.2f}B realized P&L).")

    os.makedirs("data", exist_ok=True)
    with open("data/v2_ai_tuning.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "selected_profile": winner_name,
                "overrides": winning_overrides,
                "leaderboard": results,
            },
            f,
            indent=2,
        )

    if args.promote:
        if winning_overrides:
            with open("services/ledgers.py", "r", encoding="utf-8") as f:
                led_src = f.read()
            for desk, updates in winning_overrides.items():
                for param, val in updates.items():
                    pattern = rf'("{desk}":\s*DeskProfile\([\s\S]*?\b{param}=)([^\s,)]+)'
                    led_src, count = re.subn(pattern, rf'\g<1>{val}', led_src, count=1)
                    if count == 1 and param == "min_margin" and desk == "hold":
                        led_src, _ = re.subn(
                            rf'("{desk}":\s*DeskProfile\([\s\S]*?\blive_min_margin=)([^\s,)]+)',
                            rf'\g<1>{val}',
                            led_src,
                            count=1,
                        )
            with open("services/ledgers.py", "w", encoding="utf-8") as f:
                f.write(led_src)
            print(f"[+] Applied winning '{winner_name}' DeskProfile parameters to services/ledgers.py.")

        set_active_strategy_version("v2")
        print("[+] Promoted Strategy 'v2' to ACTIVE PRODUCTION in data/strategy_config.json!")


if __name__ == "__main__":
    main()
