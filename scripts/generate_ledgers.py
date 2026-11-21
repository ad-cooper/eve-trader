#!/usr/bin/env python3
"""
EVE Online Multi-Desk Waterfall Allocator (Dynamic Equity & Cascading Slack)
=============================================================================
Order of Priority:
1. Hold Desk (24% base): Deep cyclical saturation dips (1-3 weeks).
2. Tactical Desk (16% base + Hold slack): High-dump exploration/loot capture (3-5 days).
3. Evergreen Desk (60% base + all remaining slack): Structural workhorses (24-48h).
"""

import os
import sys
import argparse
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.storage import read_json

from services.blacklist import load_active_blacklist
from services.ledgers import (
    DESK_PROFILES,
    IncumbentState,
    resolve_desk_incumbents,
    build_universe_exclusion_mask,
    select_desk_candidates,
    compute_candidate_pricing,
    compute_line_sizing,
)

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
OUTPUT_DIR = "ledgers"
MARKET_CACHE_FILE = "ledgers/market_prices_cache.json"

DESK_RATIOS = {k: cfg.ratio for k, cfg in DESK_PROFILES.items()}
DEFAULT_FALLBACK_BUDGET = 50.0e9


def resolve_dynamic_budget(capital_arg: str) -> float:
    """
    Resolves total trading budget.
    - If explicit number (e.g. '75.0', '120'): converts billions to ISK.
    - If 'auto': queries live character wallet balance + active buy escrow.
    """
    if capital_arg and capital_arg.lower() != "auto":
        try:
            val = float(capital_arg)
            return val * 1e9 if val < 1000.0 else val
        except ValueError:
            print(f"[!] Invalid capital argument '{capital_arg}', falling back to auto resolution.")

    wallet = 0.0
    escrow = 0.0

    try:
        from services.esi import fetch_wallet_balance, fetch_character_orders
        wallet = float(fetch_wallet_balance() or 0.0)
        orders = fetch_character_orders() or {}
        escrow = float(orders.get("total_escrow", 0.0))
    except Exception as e:
        print(f"[!] Live ESI equity lookup failed: {e}")

    if escrow <= 0.0:
        for orders_path in ("ledgers/character_orders.json", "data/character_orders.json"):
            c_orders = read_json(orders_path) or {}
            cached_escrow = float(c_orders.get("total_escrow", 0.0))
            if cached_escrow > 0:
                print(f"[*] Recovered {cached_escrow/1e9:.2f}B ISK escrow from local cache ({orders_path})")
                escrow = cached_escrow
                break

    if wallet <= 0.0:
        for wallet_path in ("data/wallet_cache.json", "ledgers/wallet_cache.json"):
            w_data = read_json(wallet_path) or {}
            cached_wallet = float(w_data.get("balance", 0.0))
            if cached_wallet > 0:
                print(f"[*] Recovered {cached_wallet/1e9:.2f}B ISK wallet from local cache ({wallet_path})")
                wallet = cached_wallet
                break

    total_equity = wallet + escrow
    if total_equity > 1.0e9:
        print(f"[*] Dynamic equity resolved: {wallet/1e9:.2f}B Liquid + {escrow/1e9:.2f}B Escrow = {total_equity/1e9:.2f}B ISK")
        return total_equity

    print(f"[*] Using fallback budget: {DEFAULT_FALLBACK_BUDGET/1e9:.1f}B ISK")
    return DEFAULT_FALLBACK_BUDGET


def load_cached_market_telemetry() -> dict:
    return read_json(MARKET_CACHE_FILE, default={})


def load_claimed_ids_from_csv(filename: str) -> set:
    path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            return set(df["type_id"].dropna().astype(int).tolist())
        except Exception:
            pass
    return set()


def load_incumbent_positions() -> tuple[dict, dict, dict, dict]:
    """
    Loads active buy orders, active sell orders, hangar asset quantities, and prior desk ownership
    so rebuilds keep existing orders/inventory sticky instead of rotating them out.
    """
    buys, sells, assets, prior_desks = {}, {}, {}, {}

    odata = read_json("ledgers/character_orders.json") or {}
    buys = {int(k): v for k, v in (odata.get("buys") or {}).items() if str(k).isdigit()}
    sells = {int(k): v for k, v in (odata.get("sells") or {}).items() if str(k).isdigit()}

    adata = read_json("ledgers/character_assets.json")
    if isinstance(adata, list):
        for row in adata:
            tid = row.get("type_id")
            if tid is not None:
                assets[int(tid)] = assets.get(int(tid), 0) + int(row.get("quantity", 1))

    for dname in ("hold", "tactical", "evergreen"):
        for tid in load_claimed_ids_from_csv(f"{dname}_ledger.csv"):
            prior_desks[int(tid)] = dname

    return buys, sells, assets, prior_desks


def load_market_universe():
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")

    query = """
    WITH metrics_180d AS (
        SELECT 
            type_id,
            COUNT(*) AS total_history_days,
            MAX(average) AS high_180d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_180d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL 180 DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_30d AS (
        SELECT 
            type_id,
            AVG(volume) AS daily_vol_30d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_30d,
            AVG((highest - lowest) / NULLIF(average, 0)) * 100.0 AS avg_spread_30d,
            SUM(volume * CASE WHEN highest > lowest THEN LEAST(0.95, GREATEST(0.02, (highest - LEAST(GREATEST(average, lowest), highest)) / (highest - lowest))) ELSE 0.25 END) / NULLIF(SUM(volume), 0) AS bid_share_30d,
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
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_7d AS (
        SELECT 
            type_id,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_7d,
            MIN(lowest) AS min_low_7d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL 7 DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_floors AS (
        SELECT
            type_id,
            QUANTILE_CONT(CASE WHEN date >= (SELECT MAX(date) - INTERVAL 14 DAY FROM forge_market_history) THEN lowest END, 0.25) AS min_low_14d,
            QUANTILE_CONT(lowest, 0.20) AS p20_low_30d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history) AND volume > 0
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
        COALESCE(m.bid_share_30d, 0.35) AS bid_share_30d,
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
    df = con.execute(query).df()
    con.close()
    return df


def load_recent_losing_tids(min_loss_isk: float = 1_000_000.0) -> set[int]:
    if not os.path.exists("ledgers/performance_cache.json"):
        return set()
    try:
        with open("ledgers/performance_cache.json", "r", encoding="utf-8") as pf:
            perf_items = json.load(pf).get("items", [])
        return {
            int(it["type_id"]) for it in perf_items
            if float(it.get("profit", 0.0)) < -abs(min_loss_isk)
        }
    except Exception:
        return set()


def allocate_desk_rows(
    candidates: pd.DataFrame,
    desk_type: str,
    budget: float,
    market_telemetry: dict,
    desk_params: dict,
    incumbents: IncumbentState,
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

        pricing = compute_candidate_pricing(row, desk_type, p_info, incumbents, is_sticky)
        if pricing is None:
            continue

        sizing = compute_line_sizing(
            row=row,
            pricing=pricing,
            desk_params=desk_params,
            incumbents=incumbents,
            is_sticky=is_sticky,
            passes_desk=passes_desk,
            remaining_budget=remaining_budget,
        )
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
        })

        if allocated_cap >= (budget * 0.995):
            break

    return ledger_rows, allocated_cap, new_claimed_ids, sticky_kept


def persist_desk_ledger(
    desk_type: str,
    ledger_rows: list[dict],
    allocated_cap: float,
    budget: float,
    sticky_kept: int,
) -> None:
    """Writes the desk CSV ledger and appends a timestamped snapshot to DuckDB."""
    out_file = os.path.join(OUTPUT_DIR, f"{desk_type}_ledger.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df_ledger = pd.DataFrame(ledger_rows)
    df_ledger.to_csv(out_file, index=False)
    print(f"[+] Saved {len(ledger_rows)} lines to {out_file} ({sticky_kept} sticky incumbents kept | Allocated: {allocated_cap/1e9:.2f}B ISK / {budget/1e9:.2f}B target)")

    try:
        import datetime
        con = duckdb.connect(DUCKDB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS ledger_history (
                snapshot_time TIMESTAMP,
                desk_type VARCHAR,
                type_id INTEGER,
                name VARCHAR,
                qty INTEGER,
                dump_freq DOUBLE,
                target_buy DOUBLE,
                target_sell DOUBLE,
                net_margin DOUBLE,
                capital DOUBLE,
                profit DOUBLE
            );
        """)
        df_history = df_ledger.copy()
        df_history.insert(0, "desk_type", desk_type)
        df_history.insert(0, "snapshot_time", datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0))
        con.execute("INSERT INTO ledger_history SELECT * FROM df_history")
        con.close()
    except Exception as e:
        print(f"[!] Failed to log history to DuckDB: {e}")


def generate_desk_ledger(
    df: pd.DataFrame,
    desk_type: str,
    budget: float,
    market_telemetry: dict,
    claimed_ids: set,
    total_portfolio: float = 50.0e9,
) -> tuple:
    scale_factor = max(1.0, min(2.5, total_portfolio / 50.0e9))
    active_buys, active_sells, asset_qtys, prior_desks = load_incumbent_positions()
    bl_items, bl_groups, bl_meta = load_active_blacklist()

    incumbents = resolve_desk_incumbents(
        df=df,
        desk_type=desk_type,
        claimed_ids=claimed_ids,
        active_buys=active_buys,
        active_sells=active_sells,
        asset_qtys=asset_qtys,
        prior_desks=prior_desks,
    )
    base_mask = build_universe_exclusion_mask(
        df=df,
        claimed_ids=claimed_ids,
        incumbents=incumbents,
        bl_items=bl_items,
        bl_groups=bl_groups,
        bl_meta=bl_meta,
        losing_tids=load_recent_losing_tids(),
    )
    candidates, desk_params = select_desk_candidates(
        df=df,
        desk_type=desk_type,
        base_mask=base_mask,
        desk_sticky_ids=incumbents.desk_sticky_ids,
        scale_factor=scale_factor,
    )
    ledger_rows, allocated_cap, new_claimed_ids, sticky_kept = allocate_desk_rows(
        candidates=candidates,
        desk_type=desk_type,
        budget=budget,
        market_telemetry=market_telemetry,
        desk_params=desk_params,
        incumbents=incumbents,
    )
    persist_desk_ledger(desk_type, ledger_rows, allocated_cap, budget, sticky_kept)
    return new_claimed_ids, allocated_cap

DESK_ORDER = ("hold", "tactical", "evergreen")


def load_existing_desk_state(desk_type: str) -> tuple[list[int], float]:
    """Reads claimed type_ids and deployed capital from an existing desk CSV."""
    path = os.path.join(OUTPUT_DIR, f"{desk_type}_ledger.csv")
    if not os.path.exists(path):
        return [], 0.0
    try:
        df_existing = pd.read_csv(path)
        if df_existing.empty:
            return [], 0.0
        ids = df_existing["type_id"].dropna().astype(int).tolist()
        cap = float(df_existing["capital"].fillna(0.0).sum()) if "capital" in df_existing.columns else 0.0
        return ids, cap
    except Exception:
        return [], 0.0


def run_waterfall(
    df: pd.DataFrame,
    total_budget: float,
    market_telemetry: dict,
    target_desks: set[str],
) -> dict[str, tuple[list[int], float]]:
    """
    Executes the multi-desk waterfall in priority order (hold -> tactical -> evergreen).
    Target desks are regenerated; upstream non-target desks supply their existing claimed IDs and slack.
    """
    claimed_ids: set[int] = set()
    carried_slack = 0.0
    outcomes: dict[str, tuple[list[int], float]] = {}

    base_summary = " | ".join(
        f"{d.capitalize()} {(total_budget * DESK_RATIOS[d])/1e9:.2f}B" for d in DESK_ORDER
    )
    print(f"\n=== Running Dynamic Multi-Desk Waterfall ({total_budget/1e9:.2f}B ISK Portfolio) ===")
    print(f"[*] Base Allocation Targets: {base_summary}")

    for desk in DESK_ORDER:
        if not (target_desks - set(outcomes.keys())):
            break

        target_budget = (total_budget * DESK_RATIOS[desk]) + carried_slack
        if carried_slack > 0 and desk in target_desks:
            print(f"[*] Cascading {carried_slack/1e9:.2f}B ISK slack -> {desk.capitalize()} Target: {target_budget/1e9:.2f}B ISK")

        if desk in target_desks:
            ids, deployed_cap = generate_desk_ledger(
                df, desk, target_budget, market_telemetry, claimed_ids, total_budget
            )
        else:
            ids, deployed_cap = load_existing_desk_state(desk)

        claimed_ids.update(ids)
        carried_slack = max(0.0, target_budget - deployed_cap)
        outcomes[desk] = (ids, deployed_cap)

    total_deployed = sum(cap for _, cap in outcomes.values())
    counts_summary = ", ".join(f"{len(outcomes[d][0])} {d.capitalize()}" for d in DESK_ORDER if d in outcomes)
    print(f"\n=== Waterfall Complete: {counts_summary} ===")
    print(f"[+] Total Portfolio Deployed: {total_deployed/1e9:.2f}B / {total_budget/1e9:.2f}B ISK ({(total_deployed/total_budget)*100:.1f}%)")
    return outcomes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desk", default="all", choices=["evergreen", "tactical", "hold", "all"])
    parser.add_argument("--capital", default="auto", help="Total portfolio capital in billions or 'auto' (e.g. 50.0, 89.5, 120.0, auto)")
    args = parser.parse_args()

    total_budget = resolve_dynamic_budget(args.capital)
    market_telemetry = load_cached_market_telemetry()
    df = load_market_universe()

    target_desks = set(DESK_ORDER) if args.desk == "all" else {args.desk}
    run_waterfall(df, total_budget, market_telemetry, target_desks)


if __name__ == "__main__":
    main()
