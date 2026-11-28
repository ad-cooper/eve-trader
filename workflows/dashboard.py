import os
import json
from services.esi import fetch_wallet_balance, load_sso_tokens
from services.accounting import load_performance
from services.ledgers import load_sniper_deals, load_cached_orders
from services.order_advisor import get_annotated_desks
from workflows.scheduler import STATE, get_last_ledger_run_time, get_last_sniper_run_time
from workflows.order_actions import get_order_actions


def _desk_summary(desk_rows: list[dict]) -> dict:
    cap = sum(r.get("capital", 0) for r in desk_rows)
    prof = sum(r.get("profit", 0) for r in desk_rows)
    return {"cap": cap, "prof": prof, "roi": (prof / cap * 100) if cap > 0 else 0.0}


def build_dashboard_state() -> dict:
    """Assembles the unified state payload consumed by the Preact frontend."""
    eg, tac, hld = get_annotated_desks()
    orders = load_cached_orders()
    sso = load_sso_tokens() or {}

    metrics_eg = _desk_summary(eg)
    metrics_tac = _desk_summary(tac)
    metrics_hld = _desk_summary(hld)

    total_cap = metrics_eg["cap"] + metrics_tac["cap"] + metrics_hld["cap"]
    total_prof = metrics_eg["prof"] + metrics_tac["prof"] + metrics_hld["prof"]
    all_rows = eg + tac + hld
    covered = sum(1 for r in all_rows if r.get("char_status") in ("BUY_ACTIVE", "SELL_ACTIVE"))

    sniper_cache = {}
    if os.path.exists("ledgers/live_sniper_cache.json"):
        try:
            with open("ledgers/live_sniper_cache.json", "r", encoding="utf-8") as f:
                sniper_cache = json.load(f)
        except Exception:
            pass

    wallet_balance = fetch_wallet_balance()
    active_escrow = orders.get("total_escrow", 0.0)

    actions_summary = get_order_actions(desk="all", refresh_live=False)

    return {
        "status": {**STATE, "last_ledger_run": get_last_ledger_run_time()},
        "character": {
            "name": sso.get("character_name", "Hippie Father"),
            "character_id": sso.get("character_id"),
            "is_linked": bool(sso.get("character_id")),
            "orders_count": orders.get("count", 0),
            "total_escrow": active_escrow,
            "ledger_coverage": f"{covered} / {len(all_rows)}",
        },
        "metrics": {
            "total_capital": total_cap,
            "total_profit": total_prof,
            "blended_roi": (total_prof / total_cap * 100) if total_cap > 0 else 0.0,
            "liquid_reserve": wallet_balance,
            "portfolio_budget": (wallet_balance + active_escrow) if wallet_balance > 0 else total_cap,
            "order_slots": len(all_rows),
        },
        "action_counts": {
            "bumps": actions_summary["bump_count"],
            "cancels": actions_summary["cancel_count"],
            "total": actions_summary["total_actions"],
        },
        "desks": {"evergreen": eg, "tactical": tac, "hold": hld},
        "desk_metrics": {"evergreen": metrics_eg, "tactical": metrics_tac, "hold": metrics_hld},
        "sniper": {
            "timestamp": get_last_sniper_run_time(),
            "deals": load_sniper_deals(),
            "raw_output": sniper_cache.get("raw_output", ""),
        },
        "performance": load_performance(),
    }
