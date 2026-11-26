#!/usr/bin/env python3
"""
Side-by-Side Strategy Version Comparator (v1 vs v2 vs AI Candidates)
====================================================================
Compares live/shadow CSV ledgers and forward walk-forward DuckDB ledger_history
backtests across registered strategy versions (`v1`, `v2`, `ai-*`).
"""

import os
import sys
import argparse
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.strategy_versions import (
    list_strategy_versions,
    get_active_strategy_version,
    get_version_output_dir,
)

from constants import (
    DESK_ORDER,
    DUCKDB_PATH
)


def load_version_ledgers(version: str) -> dict[str, pd.DataFrame]:
    out_dir = get_version_output_dir(version)
    frames: dict[str, pd.DataFrame] = {}
    for desk in DESK_ORDER:
        path = os.path.join(out_dir, f"{desk}_ledger.csv")
        if os.path.exists(path):
            try:
                frames[desk] = pd.read_csv(path)
            except Exception:
                frames[desk] = pd.DataFrame()
        else:
            frames[desk] = pd.DataFrame()
    return frames


def compare_current_snapshots(versions: list[str]) -> None:
    print("\n" + "=" * 96)
    print(f"SIDE-BY-SIDE LEDGER COMPARISON (Active Production: {get_active_strategy_version().upper()})")
    print("=" * 96)

    all_data = {v: load_version_ledgers(v) for v in versions}

    header = f"{'DESK':<10} | {'VERSION':<12} | {'LINES':>5} | {'CAPITAL':>11} | {'EST PROFIT':>11} | {'BLENDED ROI':>11} | {'SUPPLY MIX (MFG / LP / RNG)'}"
    print(header)
    print("-" * len(header))

    for desk in DESK_ORDER:
        for v in versions:
            df = all_data[v].get(desk, pd.DataFrame())
            if df.empty:
                print(f"{desk.upper():<10} | {v:<12} | {0:>5} | {'0.00B':>11} | {'+0.00B':>11} | {'0.00%':>11} | --")
                continue
            cap = float(df["capital"].sum()) if "capital" in df.columns else 0.0
            prof = float(df["profit"].sum()) if "profit" in df.columns else 0.0
            roi = (prof / cap * 100.0) if cap > 0 else 0.0
            if "supply_class" in df.columns:
                sc = df["supply_class"].value_counts().to_dict()
                mix = f"MFG:{sc.get('MANUFACTURED', 0)} LP:{sc.get('LP_FACTION', 0)} RNG:{sc.get('FARMED_RNG', 0)}"
            else:
                mix = "LEGACY_V1"
            print(f"{desk.upper():<10} | {v:<12} | {len(df):>5} | {cap/1e9:>9.2f}B | {prof/1e9:>+9.2f}B | {roi:>10.2f}% | {mix}")
        print("-" * len(header))

    if len(versions) >= 2:
        base_v, cand_v = versions[0], versions[1]
        print(f"\n=== DIFF HIGHLIGHTS: {cand_v.upper()} vs {base_v.upper()} ===")
        for desk in DESK_ORDER:
            df_a = all_data[base_v].get(desk, pd.DataFrame())
            df_b = all_data[cand_v].get(desk, pd.DataFrame())
            ids_a = set(df_a["type_id"].astype(int)) if (not df_a.empty and "type_id" in df_a.columns) else set()
            ids_b = set(df_b["type_id"].astype(int)) if (not df_b.empty and "type_id" in df_b.columns) else set()
            overlap = len(ids_a & ids_b)
            only_b = df_b[df_b["type_id"].isin(ids_b - ids_a)] if not df_b.empty else pd.DataFrame()
            sample_adds = ", ".join(only_b["name"].head(4).tolist()) if not only_b.empty else "None"
            print(f"  {desk.upper():<10}: {overlap} shared | {len(ids_b - ids_a)} new in {cand_v} | Top {cand_v} adds: {sample_adds}")


def compare_forward_backtest(versions: list[str], horizon_days: int = 7) -> None:
    if not os.path.exists(DUCKDB_PATH):
        return
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "ledger_history" not in tables or "forge_market_history" not in tables:
        con.close()
        return

    cols = {r[1] for r in con.execute("PRAGMA table_info('ledger_history')").fetchall()}
    if "strategy_version" not in cols:
        con.close()
        return

    placeholders = ", ".join(f"'{v}'" for v in versions)
    query = f"""
    WITH snaps AS (
        SELECT
            COALESCE(strategy_version, 'v1') AS strategy_version,
            snapshot_time,
            CAST(snapshot_time AS DATE) AS snap_date,
            desk_type,
            type_id,
            qty,
            target_buy,
            target_sell,
            capital
        FROM ledger_history
        WHERE COALESCE(strategy_version, 'v1') IN ({placeholders})
          AND CAST(snapshot_time AS DATE) <= (SELECT MAX(date) - INTERVAL {int(horizon_days)} DAY FROM forge_market_history)
    ),
    fwd AS (
        SELECT
            s.strategy_version,
            s.snapshot_time,
            s.desk_type,
            s.type_id,
            s.qty,
            s.target_buy,
            s.target_sell,
            s.capital,
            MIN(h.lowest) AS min_low,
            MAX(h.highest) AS max_high,
            AVG(h.average) AS avg_fwd
        FROM snaps s
        JOIN forge_market_history h
          ON s.type_id = h.type_id
         AND h.date > s.snap_date
         AND h.date <= s.snap_date + INTERVAL {int(horizon_days)} DAY
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
    )
    SELECT
        strategy_version,
        desk_type,
        COUNT(*) AS evaluated_lines,
        ROUND(AVG(CASE WHEN min_low <= target_buy THEN 100.0 ELSE 0.0 END), 1) AS fill_rate_pct,
        ROUND(AVG(CASE WHEN min_low <= target_buy AND max_high >= target_sell THEN 100.0 ELSE 0.0 END), 1) AS roundtrip_pct,
        SUM(
            CASE
                WHEN min_low <= target_buy AND max_high >= target_sell
                    THEN ((target_sell * 0.95395) - (target_buy * 1.005)) * qty
                WHEN min_low <= target_buy
                    THEN ((avg_fwd * 0.95395) - (target_buy * 1.005)) * qty
                ELSE 0.0
            END
        ) AS est_realized_pnl
    FROM fwd
    GROUP BY 1, 2
    ORDER BY 2, 1;
    """
    df_bt = con.execute(query).df()
    con.close()

    if df_bt.empty:
        print(f"\n[*] No matured {horizon_days}d forward snapshots in ledger_history yet (run `--version all` daily to accumulate walk-forward history).")
        return

    print(f"\n=== WALK-FORWARD RETROSPECTIVE ({horizon_days}-DAY HORIZON) ===")
    print(f"{'DESK':<10} | {'VERSION':<12} | {'LINES':>6} | {'FILL %':>7} | {'HIT TARGET %':>12} | {'EST REALIZED PNL':>16}")
    print("-" * 76)
    for _, r in df_bt.iterrows():
        print(
            f"{str(r['desk_type']).upper():<10} | {r['strategy_version']:<12} | {int(r['evaluated_lines']):>6} | "
            f"{r['fill_rate_pct']:>6.1f}% | {r['roundtrip_pct']:>11.1f}% | {float(r['est_realized_pnl'])/1e9:>+14.2f}B"
        )


def main():
    parser = argparse.ArgumentParser(description="Compare strategy versions side-by-side.")
    parser.add_argument("--versions", default="", help="Comma-separated versions (default: all registered versions)")
    parser.add_argument("--horizon", type=int, default=7, help="Forward backtest window in days (default: 7)")
    args = parser.parse_args()

    versions = [v.strip().lower() for v in args.versions.split(",") if v.strip()] if args.versions else list_strategy_versions()
    compare_current_snapshots(versions)
    compare_forward_backtest(versions, horizon_days=args.horizon)


if __name__ == "__main__":
    main()
