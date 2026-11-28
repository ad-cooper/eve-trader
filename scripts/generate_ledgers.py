#!/usr/bin/env python3
"""
EVE Online Multi-Desk Waterfall Allocator (Dynamic Equity & Cascading Slack)
=============================================================================
Order of Priority:
1. Hold Desk (24% base): Deep cyclical saturation dips (1-3 weeks).
2. Tactical Desk (16% base + Hold slack): High-dump exploration/loot capture (3-5 days).
3. Evergreen Desk (60% base + all remaining slack): Structural workhorses (24-48h).
"""

import argparse
import datetime
import os
import sys

import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from constants import (
    DEFAULT_FALLBACK_BUDGET,
    DESK_ORDER,
    DUCKDB_PATH,
    OUTPUT_DIR,
    SQLITE_PATH,
)
from services.desk import Desk, IncumbentState
from services.desk_config import BUDGET_FILL_STOP_RATIO
from services.market_universe import (
    build_universe_exclusion_mask,
    load_market_universe    
)
from services.market import (
    load_cached_market_telemetry
)
from services.strategy_versions import (
    StrategyVersion,
    get_active_strategy_version,
    get_strategy_version,
    list_strategy_versions,
)
from services.accounting import load_recent_losing_typeids

def resolve_dynamic_budget(capital_arg: str) -> float:
    """Resolves total trading budget from an explicit billion/ISK value or fallback."""
    if capital_arg and capital_arg.lower() != "auto":
        try:
            val = float(capital_arg)
            return val * 1e9 if val < 1000.0 else val
        except ValueError:
            print(f"[!] Invalid capital argument '{capital_arg}', falling back to default.")

    print(f"[*] Using fallback budget: {DEFAULT_FALLBACK_BUDGET/1e9:.1f}B ISK")
    return DEFAULT_FALLBACK_BUDGET

def allocate_desk_rows(
    candidates: pd.DataFrame,
    desk_type: str,
    budget: float,
    market_telemetry: dict,
    desk_params: dict,
    incumbents: IncumbentState,
    strat: StrategyVersion,
) -> tuple[list[dict], float, list[int], int]:
    """Assembles ledger row dicts for ranked candidates up to the desk ISK budget."""
    ledger_rows: list[dict] = []
    allocated_cap = 0.0
    new_claimed_ids: list[int] = []
    sticky_kept = 0

    for _, row in candidates.iterrows():
        remaining_budget = budget - allocated_cap
        if remaining_budget <= 0:
            break

        tid = int(row["type_id"])
        is_sticky = bool(row.get("is_sticky", False))
        passes_desk = bool(row.get("passes_desk", False))
        p_info = market_telemetry.get(str(tid)) or {}

        pricing = strat.price_candidate(row, desk_type, p_info, incumbents, is_sticky)
        if pricing is None:
            continue

        sizing = strat.size_line(row, pricing, desk_params, incumbents, is_sticky, passes_desk, remaining_budget)
        if sizing is None:
            continue

        allocated_cap += sizing["capital"]
        new_claimed_ids.append(tid)
        if is_sticky:
            sticky_kept += 1

        ledger_rows.append({
            "type_id": tid,
            "name": row["item_name"],
            "qty": sizing["qty"],
            "dump_freq": round(pricing["dump_freq"], 1),
            "target_buy": round(pricing["target_buy"], 2),
            "target_sell": round(pricing["target_sell"], 2),
            "net_margin": round(pricing["net_margin"], 2),
            "capital": round(sizing["capital"], 2),
            "profit": round(sizing["profit"], 2),
            "allow_new_buy": bool(sizing.get("allow_new_buy", True)),
            "supply_class": str(row.get("supply_class") or "LEGACY_V1"),
        })

        if allocated_cap >= (budget * BUDGET_FILL_STOP_RATIO):
            break

    return ledger_rows, allocated_cap, new_claimed_ids, sticky_kept


def generate_desk_ledger(
    df: pd.DataFrame,
    desk_type: str,
    budget: float,
    market_telemetry: dict,
    claimed_ids: set,
    total_portfolio: float = 50.0e9,
    strategy_version: str = "v1",
    snapshot_time=None,
    desk: Desk | None = None,
    strat: StrategyVersion | None = None,
) -> tuple[list[int], float]:
    strat_obj = strat or get_strategy_version(strategy_version)
    scale_factor = max(1.0, min(2.5, total_portfolio / 50.0e9))
    desk_obj = desk or Desk(
        desk_type=desk_type,
        strategy_version=strategy_version,
        output_dir=OUTPUT_DIR,
        db_path=DUCKDB_PATH,
    )
    incumbents = desk_obj.resolve_incumbents(df=df, claimed_ids=claimed_ids)
    base_mask = build_universe_exclusion_mask(
        df=df,
        claimed_ids=claimed_ids,
        incumbents=incumbents,
        losing_tids=load_recent_losing_typeids(),
    )
    candidates, desk_params = strat_obj.select_candidates(
        df,
        desk_obj.desk_type,
        base_mask,
        incumbents.desk_sticky_ids,
        scale_factor,
    )
    ledger_rows, allocated_cap, new_claimed_ids, sticky_kept = allocate_desk_rows(
        candidates=candidates,
        desk_type=desk_obj.desk_type,
        budget=budget,
        market_telemetry=market_telemetry,
        desk_params=desk_params,
        incumbents=incumbents,
        strat=strat_obj,
    )
    desk_obj.persist_ledger(
        ledger_rows=ledger_rows,
        allocated_cap=allocated_cap,
        budget=budget,
        sticky_kept=sticky_kept,
        snapshot_time=snapshot_time,
    )
    return new_claimed_ids, allocated_cap


def run_waterfall(
    df: pd.DataFrame,
    total_budget: float,
    market_telemetry: dict,
    target_desks: set[str],
    strategy_version: str = "v1",
    snapshot_time=None,
) -> dict[str, tuple[list[int], float]]:
    """
    Executes the multi-desk waterfall in priority order (hold -> tactical -> evergreen).
    Target desks are regenerated; upstream non-target desks supply their existing claimed IDs and slack.
    """
    claimed_ids: set[int] = set()
    carried_slack = 0.0
    outcomes: dict[str, tuple[list[int], float]] = {}

    strat = get_strategy_version(strategy_version)
    df_enriched = strat.enrich_universe(df, SQLITE_PATH, DUCKDB_PATH)
    desks = {
        d: Desk(
            desk_type=d,
            strategy_version=strategy_version,
            output_dir=OUTPUT_DIR,
            db_path=DUCKDB_PATH,
        )
        for d in DESK_ORDER
    }

    base_summary = " | ".join(
        f"{desks[d].desk_type.capitalize()} {(total_budget * desks[d].ratio)/1e9:.2f}B"
        for d in DESK_ORDER
    )
    print(f"\n=== Running Dynamic Multi-Desk Waterfall [{strategy_version.upper()}] ({total_budget/1e9:.2f}B ISK Portfolio) ===")
    print(f"[*] Strategy: {strat.description}")
    print(f"[*] Base Allocation Targets: {base_summary}")

    for desk_name in DESK_ORDER:
        if not (target_desks - set(outcomes.keys())):
            break

        desk_obj = desks[desk_name]
        target_budget = (total_budget * desk_obj.ratio) + carried_slack
        if carried_slack > 0 and desk_name in target_desks:
            print(f"[*] Cascading {carried_slack/1e9:.2f}B ISK slack -> {desk_name.capitalize()} Target: {target_budget/1e9:.2f}B ISK")

        if desk_name in target_desks:
            ids, deployed_cap = generate_desk_ledger(
                df_enriched,
                desk_name,
                target_budget,
                market_telemetry,
                claimed_ids,
                total_budget,
                strategy_version=strategy_version,
                snapshot_time=snapshot_time,
                desk=desk_obj,
                strat=strat,
            )
        else:
            ids, deployed_cap = desk_obj.existing_state()

        claimed_ids.update(ids)
        carried_slack = max(0.0, target_budget - deployed_cap)
        outcomes[desk_name] = (ids, deployed_cap)

    total_deployed = sum(cap for _, cap in outcomes.values())
    counts_summary = ", ".join(f"{len(outcomes[d][0])} {d.capitalize()}" for d in DESK_ORDER if d in outcomes)
    print(f"\n=== Waterfall Complete [{strategy_version.upper()}]: {counts_summary} ===")
    print(f"[+] Total Portfolio Deployed: {total_deployed/1e9:.2f}B / {total_budget/1e9:.2f}B ISK ({(total_deployed/total_budget)*100:.1f}%)")
    return outcomes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desk", default="all", choices=["evergreen", "tactical", "hold", "all"])
    parser.add_argument("--capital", default="auto", help="Total portfolio capital in billions or 'auto' (e.g. 50.0, 89.5, 120.0, auto)")
    parser.add_argument("--version", default="all", help="Strategy version to run ('v1', 'v2', 'all', or registered ai-* version)")
    args = parser.parse_args()

    total_budget = resolve_dynamic_budget(args.capital)
    market_telemetry = load_cached_market_telemetry()
    df = load_market_universe()

    target_desks = set(DESK_ORDER) if args.desk == "all" else {args.desk}
    shared_snapshot_time = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

    if args.version.lower() == "all":
        active_v = get_active_strategy_version()
        versions_to_run = [active_v] + [v for v in list_strategy_versions() if v != active_v]
    else:
        versions_to_run = [v.strip().lower() for v in args.version.split(",") if v.strip()]

    for ver in versions_to_run:
        try:
            run_waterfall(
                df,
                total_budget,
                market_telemetry,
                target_desks,
                strategy_version=ver,
                snapshot_time=shared_snapshot_time,
            )
        except Exception as e:
            if ver == get_active_strategy_version():
                raise
            print(f"[!] Shadow version '{ver}' failed (non-blocking): {e}")


if __name__ == "__main__":
    main()