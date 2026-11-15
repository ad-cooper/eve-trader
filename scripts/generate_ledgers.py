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
import json
import argparse
import duckdb
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.blacklist import load_active_blacklist

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
OUTPUT_DIR = "ledgers"
MARKET_CACHE_FILE = "ledgers/market_prices_cache.json"

BUY_FEE_CITADEL = 0.0050   # 0.50% SCC surcharge (Citadel 1-jump)
SELL_FEE_JITA = 0.0123     # 1.23% Broker Relations V
SALES_TAX_JITA = 0.03375   # 3.375% Accounting V
TOTAL_EXIT_FEES = SELL_FEE_JITA + SALES_TAX_JITA  # 4.605%

DESK_RATIOS = {
    "hold": 0.24,       # 24% of portfolio
    "tactical": 0.16,   # 16% of portfolio
    "evergreen": 0.60   # 60% of portfolio (+ 100% of unused slack)
}
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
        for orders_path in ["ledgers/character_orders.json", "data/character_orders.json"]:
            if os.path.exists(orders_path):
                try:
                    with open(orders_path, "r", encoding="utf-8") as f:
                        c_orders = json.load(f)
                        cached_escrow = float(c_orders.get("total_escrow", 0.0))
                        if cached_escrow > 0:
                            print(f"[*] Recovered {cached_escrow/1e9:.2f}B ISK escrow from local cache ({orders_path})")
                            escrow = cached_escrow
                            break
                except Exception:
                    pass

    if wallet <= 0.0:
        for wallet_path in ["data/wallet_cache.json", "ledgers/wallet_cache.json"]:
            if os.path.exists(wallet_path):
                try:
                    with open(wallet_path, "r", encoding="utf-8") as f:
                        w_data = json.load(f)
                        cached_wallet = float(w_data.get("balance", 0.0))
                        if cached_wallet > 0:
                            print(f"[*] Recovered {cached_wallet/1e9:.2f}B ISK wallet from local cache ({wallet_path})")
                            wallet = cached_wallet
                            break
                except Exception:
                    pass

    total_equity = wallet + escrow
    if total_equity > 1.0e9:
        print(f"[*] Dynamic equity resolved: {wallet/1e9:.2f}B Liquid + {escrow/1e9:.2f}B Escrow = {total_equity/1e9:.2f}B ISK")
        return total_equity

    print(f"[*] Using fallback budget: {DEFAULT_FALLBACK_BUDGET/1e9:.1f}B ISK")
    return DEFAULT_FALLBACK_BUDGET


def load_cached_market_telemetry() -> dict:
    if os.path.exists(MARKET_CACHE_FILE):
        try:
            with open(MARKET_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


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

    if os.path.exists("ledgers/character_orders.json"):
        try:
            with open("ledgers/character_orders.json", "r", encoding="utf-8") as f:
                odata = json.load(f)
            buys = {int(k): v for k, v in (odata.get("buys") or {}).items() if str(k).isdigit()}
            sells = {int(k): v for k, v in (odata.get("sells") or {}).items() if str(k).isdigit()}
        except Exception:
            pass

    if os.path.exists("ledgers/character_assets.json"):
        try:
            with open("ledgers/character_assets.json", "r", encoding="utf-8") as f:
                adata = json.load(f)
            if isinstance(adata, list):
                for row in adata:
                    tid = row.get("type_id")
                    if tid is not None:
                        assets[int(tid)] = assets.get(int(tid), 0) + int(row.get("quantity", 1))
        except Exception:
            pass

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
            (COUNT(CASE WHEN (average - lowest) / NULLIF(average, 0) >= 0.045 THEN 1 END) * 100.0 / COUNT(*)) AS dump_freq_pct
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


def generate_desk_ledger(df: pd.DataFrame, desk_type: str, budget: float, market_telemetry: dict, claimed_ids: set, total_portfolio: float = 50.0e9) -> tuple:
    scale_factor = max(1.0, min(2.5, total_portfolio / 50.0e9))
    active_buys, active_sells, asset_qtys, prior_desks = load_incumbent_positions()

    # Only treat active market orders (buys/sells) or substantial hangar stacks (>= 50M ISK) as sticky incumbents
    price_lookup = dict(zip(df["type_id"].astype(int), df["price_m_30d"] * 1e6))
    significant_assets = {
        tid for tid, qty in asset_qtys.items()
        if (qty * price_lookup.get(tid, 0.0)) >= 50_000_000.0
    }
    incumbent_ids = set(active_buys.keys()) | set(active_sells.keys()) | significant_assets

    other_prior_desks = {tid for tid, d in prior_desks.items() if d != desk_type}
    if desk_type == "evergreen":
        desk_sticky_ids = (incumbent_ids - claimed_ids) - other_prior_desks
    else:
        desk_sticky_ids = {tid for tid in (incumbent_ids - claimed_ids) if prior_desks.get(tid) == desk_type}

    mask = ~df["type_id"].isin(claimed_ids)
    bl_items, bl_groups, _ = load_active_blacklist()
    mask &= ~df["group_name"].isin(bl_groups)
    mask &= ~df["item_name"].isin(bl_items)
    mask &= ~df["category_name"].isin(["Personalization", "SKINs", "Special Edition Assets", "Apparel"])
    mask &= ~df["item_name"].str.contains("Modified|Voucher|Token|Certificate", regex=True)
    mask &= ~df["item_name"].str.contains(
        "Keypass|Cipher|Shortcut|Gauntlet|Tetrimon|Chemal Tech|Harvest .*Booster|Radiance.*Accelerator",
        case=False,
        regex=True,
        na=False,
    )
    if os.path.exists("ledgers/performance_cache.json"):
        try:
            with open("ledgers/performance_cache.json", "r") as pf:
                perf_items = json.load(pf).get("items", [])
                losing_tids = {
                    int(it["type_id"]) for it in perf_items
                    if float(it.get("profit", 0.0)) < -1_000_000
                    and int(it["type_id"]) not in active_sells
                    and int(it["type_id"]) not in significant_assets
                }
                mask &= ~df["type_id"].isin(losing_tids)
        except Exception:
            pass

    is_sticky_series = df["type_id"].isin(desk_sticky_ids)
    mask &= (df["price_m_7d"] >= (df["price_m_30d"] * 0.78)) | (is_sticky_series & (df["price_m_7d"] >= (df["price_m_30d"] * 0.68)))

    if desk_type == "hold":
        desk_mask = (
            ~df["category_name"].isin(["Skill"])
            & df["price_m_180d"].between(15.0, 2500.0)
            & (df["total_history_days"] >= 60)
            & (df["daily_turnover_m"] >= 10.0)
            & (df["avg_spread_30d"] >= 8.0)
            & (df["price_m_30d"] <= (df["price_m_180d"] * 0.98))
        )
        candidates = df[mask & (desk_mask | is_sticky_series)].copy()
        candidates["passes_desk"] = desk_mask.loc[candidates.index]
        candidates["is_sticky"] = candidates["type_id"].isin(desk_sticky_ids)
        candidates["upside_pct"] = ((candidates["price_m_180d"] * 0.96) - candidates["price_m_30d"]) / candidates["price_m_30d"] * 100.0
        candidates = candidates.sort_values(by=["is_sticky", "upside_pct"], ascending=[False, False])
        vol_pct = 0.08
        max_line_cap = 1.5e9 * scale_factor
        min_line_cap = 150.0e6
        min_margin_req = 18.0
        sticky_margin_floor = 10.0

    elif desk_type == "tactical":
        desk_mask = (
            df["price_m_30d"].between(5.0, 2000.0)
            & (df["daily_turnover_m"] >= 35.0)
            & (df["dump_freq_pct"] >= 45.0)
            & (df["avg_spread_30d"] >= 10.0)
        )
        candidates = df[mask & (desk_mask | is_sticky_series)].copy()
        candidates["passes_desk"] = desk_mask.loc[candidates.index]
        candidates["is_sticky"] = candidates["type_id"].isin(desk_sticky_ids)
        candidates = candidates.sort_values(by=["is_sticky", "dump_freq_pct"], ascending=[False, False])
        vol_pct = 0.15
        max_line_cap = 1.2e9 * scale_factor
        min_line_cap = 120.0e6
        min_margin_req = 8.5
        sticky_margin_floor = 5.0

    else:  # evergreen
        desk_mask = (
            df["price_m_30d"].between(1.0, 2500.0)
            & (df["daily_turnover_m"] >= 35.0)
            & (df["daily_vol_30d"] >= 2.0)
            & (df["avg_spread_30d"] >= 9.8)
        )
        candidates = df[mask & (desk_mask | is_sticky_series)].copy()
        candidates["passes_desk"] = desk_mask.loc[candidates.index]
        candidates["is_sticky"] = candidates["type_id"].isin(desk_sticky_ids)
        candidates["profit_velocity"] = candidates["daily_turnover_m"] * (candidates["avg_spread_30d"] - 5.1)
        candidates = candidates.sort_values(by=["is_sticky", "profit_velocity"], ascending=[False, False])
        vol_pct = 0.25
        max_line_cap = 1.2e9 * scale_factor
        min_line_cap = 100.0e6
        min_margin_req = 5.0
        sticky_margin_floor = 2.5

    ledger_rows = []
    allocated_cap = 0.0
    new_claimed_ids = []
    sticky_kept = 0

    for _, row in candidates.iterrows():
        tid = int(row["type_id"])
        is_sticky = bool(row.get("is_sticky", False))
        passes_desk = bool(row.get("passes_desk", False))
        has_buy = tid in active_buys
        has_sell = tid in active_sells
        held_qty = int(asset_qtys.get(tid, 0))

        vwap_30d = row["price_m_30d"] * 1e6
        vwap_180d = row["price_m_180d"] * 1e6
        vwap_7d = row["price_m_7d"] * 1e6
        p20_low_30d = (row.get("p20_low_m_30d") or row["price_m_30d"] * 0.94) * 1e6
        dump_freq = float(row["dump_freq_pct"])

        p_info = market_telemetry.get(str(tid)) or {}
        live_ask = p_info.get("lowest_ask")
        live_top_buy = p_info.get("top_buy")

        # Skip unplaced items whose live telemetry has been polled and shows zero active sell orders
        if not has_buy and not has_sell and held_qty == 0 and str(tid) in market_telemetry and (not live_ask or live_ask <= 0):
            continue

        p_current = live_ask if (live_ask and live_ask > 0) else (vwap_7d if vwap_7d > 0 else vwap_30d)
        drift = abs(p_current - vwap_30d) / vwap_30d if vwap_30d > 0 else 0
        stability_score = max(0.70, min(1.0, 1.0 - (1.2 * drift) - (0.2 * (dump_freq / 100.0))))

        # Evaluate existing buy orders at their actual bid price instead of outbidding ourselves
        if has_buy:
            competitive_bid = float(active_buys[tid]["price"])
        elif live_top_buy and live_top_buy > 0:
            competitive_bid = live_top_buy + 0.01
        else:
            competitive_bid = min(p20_low_30d, p_current * 0.92)

        if desk_type == "hold":
            if not is_sticky and live_ask and live_ask >= (vwap_180d * 0.90):
                continue
            t_sell = min(vwap_180d * 0.960, vwap_30d * 1.220)
            t_buy = competitive_bid
            if not has_buy and live_ask and live_ask > 0:
                t_buy = min(t_buy, live_ask * 0.94)

        elif desk_type == "tactical":
            stabilized_ask = p_current * (0.95 + 0.05 * stability_score)
            baseline_sell = vwap_30d * 1.045
            t_sell = (0.50 * stabilized_ask) + (0.50 * baseline_sell)
            t_buy = competitive_bid
            if not has_buy and live_ask and live_ask > 0:
                t_buy = min(t_buy, live_ask * 0.95)

        else:  # evergreen
            stabilized_ask = p_current * (0.95 + 0.05 * stability_score)
            t_sell = min(stabilized_ask, vwap_30d * 1.055)
            t_buy = competitive_bid
            if not has_buy:
                ceil_guard = (live_ask * 0.985) if live_ask else (t_sell * 0.985)
                t_buy = min(t_buy, ceil_guard)

        if has_sell and float(active_sells[tid].get("price", 0.0)) > 0:
            t_sell = max(t_sell, float(active_sells[tid]["price"]))

        cost_unit = t_buy * (1.0 + BUY_FEE_CITADEL)
        proceeds_unit = t_sell * (1.0 - TOTAL_EXIT_FEES)
        net_profit_unit = proceeds_unit - cost_unit
        net_margin = (net_profit_unit / cost_unit) * 100.0 if cost_unit > 0 else 0.0

        req_margin = sticky_margin_floor if is_sticky else min_margin_req
        if not has_sell and held_qty == 0 and net_margin < req_margin:
            continue

        if not is_sticky and cost_unit > max_line_cap:
            continue

        raw_qty = max(1, int(row["daily_vol_30d"] * vol_pct))
        cap_qty = max(1, int(max_line_cap / cost_unit)) if cost_unit > 0 else 1
        qty = min(raw_qty, cap_qty)

        buy_rem = int(active_buys[tid].get("volume_remain", 0)) if has_buy else 0
        sell_rem = int(active_sells[tid].get("volume_remain", 0)) if has_sell else 0
        existing_qty = max(buy_rem, sell_rem + held_qty)

        if is_sticky:
            if not passes_desk:
                # Item only stayed because of existing order/stock: preserve exact existing qty without new buy bloat
                qty = max(1, existing_qty)
            else:
                qty = max(qty, existing_qty)

        capital_line = cost_unit * qty

        effective_min_cap = (min_line_cap * 0.35) if is_sticky else min_line_cap
        if not has_buy and not has_sell and held_qty == 0 and capital_line < effective_min_cap and (cost_unit * raw_qty) < effective_min_cap:
            continue

        remaining_budget = budget - allocated_cap
        if remaining_budget <= 0:
            break

        if capital_line > remaining_budget:
            if is_sticky and existing_qty > 0:
                qty = existing_qty
            else:
                qty = int(remaining_budget / cost_unit)
            if qty < 1:
                continue
            capital_line = cost_unit * qty

        profit_line = net_profit_unit * qty
        allocated_cap += capital_line
        new_claimed_ids.append(tid)
        if is_sticky:
            sticky_kept += 1

        ledger_rows.append({
            "type_id": tid,
            "name": row["item_name"],
            "qty": qty,
            "dump_freq": round(dump_freq, 1),
            "target_buy": round(t_buy, 2),
            "target_sell": round(t_sell, 2),
            "net_margin": round(net_margin, 2),
            "capital": round(capital_line, 2),
            "profit": round(profit_line, 2)
        })

        if allocated_cap >= (budget * 0.995):
            break

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

    return new_claimed_ids, allocated_cap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desk", default="all", choices=["evergreen", "tactical", "hold", "all"])
    parser.add_argument("--capital", default="auto", help="Total portfolio capital in billions or 'auto' (e.g. 50.0, 89.5, 120.0, auto)")
    args = parser.parse_args()

    total_budget = resolve_dynamic_budget(args.capital)
    market_telemetry = load_cached_market_telemetry()
    df = load_market_universe()

    if args.desk == "all":
        target_hold = total_budget * DESK_RATIOS["hold"]
        target_tac = total_budget * DESK_RATIOS["tactical"]
        target_eg = total_budget * DESK_RATIOS["evergreen"]

        print(f"\n=== Running Dynamic Multi-Desk Waterfall ({total_budget/1e9:.2f}B ISK Portfolio) ===")
        print(f"[*] Base Allocation Targets: Hold {target_hold/1e9:.2f}B | Tactical {target_tac/1e9:.2f}B | Evergreen {target_eg/1e9:.2f}B")

        claimed_ids = set()

        hold_ids, hold_cap = generate_desk_ledger(df, "hold", target_hold, market_telemetry, claimed_ids, total_budget)
        claimed_ids.update(hold_ids)
        hold_slack = max(0.0, target_hold - hold_cap)

        tac_target = target_tac + hold_slack
        if hold_slack > 0:
            print(f"[*] Hold Desk left {hold_slack/1e9:.2f}B ISK slack -> Cascading into Tactical Desk (New Target: {tac_target/1e9:.2f}B)")
        tac_ids, tac_cap = generate_desk_ledger(df, "tactical", tac_target, market_telemetry, claimed_ids, total_budget)
        claimed_ids.update(tac_ids)

        evergreen_target = max(0.0, total_budget - hold_cap - tac_cap)
        print(f"[*] Cascading remaining slack to Evergreen -> Final Target: {evergreen_target/1e9:.2f}B ISK")
        eg_ids, eg_cap = generate_desk_ledger(df, "evergreen", evergreen_target, market_telemetry, claimed_ids, total_budget)

        total_deployed = hold_cap + tac_cap + eg_cap
        print(f"\n=== Waterfall Complete: {len(hold_ids)} Hold, {len(tac_ids)} Tactical, {len(eg_ids)} Evergreen ===")
        print(f"[+] Total Portfolio Deployed: {total_deployed/1e9:.2f}B / {total_budget/1e9:.2f}B ISK ({(total_deployed/total_budget)*100:.1f}%)")

    elif args.desk == "hold":
        target = total_budget * DESK_RATIOS["hold"]
        generate_desk_ledger(df, "hold", target, market_telemetry, set(), total_budget)

    elif args.desk == "tactical":
        target = total_budget * DESK_RATIOS["tactical"]
        hold_claimed = load_claimed_ids_from_csv("hold_ledger.csv")
        generate_desk_ledger(df, "tactical", target, market_telemetry, hold_claimed, total_budget)

    elif args.desk == "evergreen":
        target = total_budget * DESK_RATIOS["evergreen"]
        hold_claimed = load_claimed_ids_from_csv("hold_ledger.csv")
        tac_claimed = load_claimed_ids_from_csv("tactical_ledger.csv")
        combined = hold_claimed.union(tac_claimed)
        generate_desk_ledger(df, "evergreen", target, market_telemetry, combined, total_budget)


if __name__ == "__main__":
    main()
