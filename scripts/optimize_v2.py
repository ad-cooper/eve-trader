#!/usr/bin/env python3
"""
Walk-Forward AI Parameter Optimizer for Strategy v2
===================================================
Evaluates candidate parameter refinements on top of `v2` (SDE supply elasticity +
book-depth filtering) using zero-lookahead point-in-time walk-forward replay
(`scripts/backtest_replay.py`). Applies winning overrides to `DESK_CONFIG_PATH`
when `--promote` is passed.
"""

from __future__ import annotations
import os
import sys
import re
import json
import argparse
import dataclasses
from contextlib import contextmanager
from pathlib import Path
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.desk_config import (
    DESK_PROFILES,
)
from scripts.backtest_replay import (
    load_sde_item_metadata,
    run_walkforward_replay,
)
from constants import (
    DESK_CONFIG_PATH,
    DUCKDB_PATH,
    SQLITE_PATH,
)


@contextmanager
def _temporary_profile_overrides(profile_overrides: dict[str, dict]):
    saved = {k: dataclasses.replace(v) for k, v in DESK_PROFILES.items()}
    try:
        for desk, updates in profile_overrides.items():
            if desk in DESK_PROFILES:
                DESK_PROFILES[desk] = dataclasses.replace(
                    DESK_PROFILES[desk], **updates
                )
        yield
    finally:
        DESK_PROFILES.clear()
        DESK_PROFILES.update(saved)


def summarize_candidate_metrics(df_trades: pd.DataFrame, name: str) -> dict:
    filled = df_trades[df_trades["filled_qty"] > 0] if not df_trades.empty else pd.DataFrame()
    deployed = float(filled["deployed_capital"].sum()) if not filled.empty else 0.0
    realized = float(filled["realized_pnl"].sum()) if not filled.empty else 0.0
    mtm = float(filled["mtm_pnl"].sum()) if not filled.empty else 0.0
    stops = int((df_trades["status"] == "TIME_STOP").sum()) if not df_trades.empty else 0
    stop_pct = (stops / max(1, len(filled))) * 100.0 if not filled.empty else 0.0
    roi = (realized / deployed * 100.0) if deployed > 0 else 0.0
    score = realized * (1.0 - min(0.5, stop_pct / 200.0))
    return {
        "candidate": name,
        "orders": len(df_trades),
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
    parser.add_argument("--promote", action="store_true", help="Apply winning v2 profile overrides to desk_config.py")
    args = parser.parse_args()

    candidates: dict[str, dict[str, dict]] = {
        "v2-baseline": {},
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

    sde_meta_df = load_sde_item_metadata(SQLITE_PATH)
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    results: list[dict] = []
    try:
        for cname, overrides in candidates.items():
            with _temporary_profile_overrides(overrides):
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
            results.append(summarize_candidate_metrics(report["trades"], cname))
    finally:
        con.close()

    print("\n" + "=" * 96)
    print(f"V2 AI WALK-FORWARD OPTIMIZATION LEADERBOARD ({args.days_back}d Replay @ {args.budget:.1f}B ISK)")
    print("=" * 96)
    print(f"{'CANDIDATE':<18} | {'FILLED':>8} | {'DEPLOYED':>10} | {'REALIZED PNL':>12} | {'ROI %':>8} | {'STOP %':>7} | {'SCORE':>10}")
    print("-" * 96)
    for r in sorted(results, key=lambda x: -x["score"]):
        print(
            f"{r['candidate']:<18} | {r['filled']:>4}/{r['orders']:<3} | {r['deployed_b']:>8.2f}B | "
            f"{r['realized_b']:>+10.2f}B | {r['roi_pct']:>7.2f}% | {r['stop_pct']:>6.1f}% | {r['score']/1e9:>+8.2f}B"
        )

    by_name = {r["candidate"]: r for r in results}
    v2_base = by_name["v2-baseline"]
    tuned_names = [k for k in candidates if k != "v2-baseline"]
    best_cand = max([by_name[c] for c in tuned_names], key=lambda x: x["score"])

    winner_name = "v2-baseline"
    winning_overrides: dict[str, dict] = {}
    if best_cand["score"] > v2_base["score"] * 1.03 and best_cand["realized_b"] > v2_base["realized_b"]:
        winner_name = best_cand["candidate"]
        winning_overrides = candidates[winner_name]
        print(f"\n[+] AI Candidate '{winner_name}' beat baseline v2 (+{best_cand['realized_b'] - v2_base['realized_b']:.2f}B realized P&L)!")
    else:
        print(f"\n[*] Baseline 'v2-baseline' outperformed or matched all parameter perturbations ({v2_base['realized_b']:+.2f}B realized P&L).")

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

    if args.promote and winning_overrides:
        cfg_path = Path(DESK_CONFIG_PATH)
        cfg_src = cfg_path.read_text(encoding="utf-8")
        for desk, updates in winning_overrides.items():
            for param, val in updates.items():
                pattern = rf'("{desk}":\s*DeskProfile\([\s\S]*?\b{param}=)([^\s,)]+)'
                cfg_src, count = re.subn(pattern, rf'\g<1>{val}', cfg_src, count=1)
                if count == 1 and param == "min_margin" and desk == "hold":
                    cfg_src, _ = re.subn(
                        rf'("{desk}":\s*DeskProfile\([\s\S]*?\blive_min_margin=)([^\s,)]+)',
                        rf'\g<1>{val}',
                        cfg_src,
                        count=1,
                    )
        cfg_path.write_text(cfg_src, encoding="utf-8")
        print(f"[+] Applied winning '{winner_name}' DeskProfile parameters to {DESK_CONFIG_PATH}.")


if __name__ == "__main__":
    main()