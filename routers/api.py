from fastapi import APIRouter, BackgroundTasks, Request

from services.esi import fetch_character_orders
from services.accounting import calculate_performance
from services.market import update_ledger_market_prices
from workflows.dashboard import build_dashboard_state
from workflows.scheduler import trigger_ledger_rebuild, trigger_sniper_scan
from workflows.buy_orders import audit_missing_buys
from workflows.sell_advisor import evaluate_hangar_inventory
from workflows.imports import (
    scan_import_opportunities,
    load_cached_import_suggestions,
    check_cj6mt_seller_stock,
    bundle_courier_contracts,
)

router = APIRouter(prefix="/api", tags=["api"])


# --- Dashboard & Workflow 1: Active Desks / Daily Sync ---

@router.get("/state")
def api_state():
    return build_dashboard_state()


@router.post("/ledgers/run")
def api_ledgers_run():
    return trigger_ledger_rebuild()


@router.post("/orders/sync-esi")
async def sync_esi():
    fetch_character_orders()
    calculate_performance()
    return {"status": "success"}


@router.post("/market/refresh-prices")
async def refresh_market_prices(bg: BackgroundTasks):
    bg.add_task(update_ledger_market_prices)
    return {"status": "started"}


# --- Workflow 2: Missing Buy Orders (MultiBuy) ---

@router.get("/orders/actions")
def api_order_actions(desk: str = "all", refresh: bool = False):
    from workflows.order_actions import get_order_actions
    return get_order_actions(desk=desk, refresh_live=refresh)


@router.get("/orders/missing-buys")
def api_missing_buys(desk: str = "all"):
    return audit_missing_buys(desk=desk)


# --- Workflow 3: Jita Hangar Sell Advisor (MultiSell) ---

@router.api_route("/inventory/advisor", methods=["GET", "POST"])
async def api_inventory_advisor(request: Request):
    raw_text = None
    force_refresh = request.query_params.get("force") == "true"
    if request.method == "POST":
        try:
            body = await request.json()
            raw_text = body.get("raw_text")
            if body.get("force"):
                force_refresh = True
        except Exception:
            pass
    return evaluate_hangar_inventory(raw_text=raw_text, force_refresh=force_refresh)


# --- Workflow 4: Cross-Region Imports & Courier Bundling ---

@router.get("/import/opportunities")
def api_import_opportunities(
    amarr_warps: int = 35,
    cj6mt_rate: float = 800.0,
    limit: int = 100,
    force: bool = False,
):
    if not force:
        cached = load_cached_import_suggestions()
        if cached and cached.get("amarr", {}).get("pricing_model") == "hisec_tiered" and cached.get("cj6mt"):
            return cached
    return scan_import_opportunities(amarr_warps=amarr_warps, cj6mt_rate=cj6mt_rate, limit=limit)


@router.get("/import/cj6mt-stock")
def api_import_cj6mt_stock():
    return check_cj6mt_seller_stock()


@router.post("/import/bundles")
def api_import_bundles(payload: dict):
    return bundle_courier_contracts(
        dest_key=payload.get("dest_key", "amarr"),
        items=payload.get("items"),
        raw_text=payload.get("raw_text"),
        from_jita_hangar=bool(payload.get("from_jita_hangar", False)),
        rate_override=payload.get("rate_override"),
    )


# --- Tools: Live Deal Sniper ---

@router.post("/sniper/run")
def api_sniper_run():
    return trigger_sniper_scan()


# --- Workflow 5: Retrospective Ledger & Algorithm Advisor (Local Walk-Forward) ---

@router.post("/retrospective/analyze")
def api_retrospective_analyze(
    lookback_days: int = 45,
    step_days: int = 7,
    total_budget_b: float = 80.0,
):
    from fastapi import HTTPException
    from services.retrospective import run_local_retrospective
    try:
        return run_local_retrospective(
            lookback_days=lookback_days,
            step_days=step_days,
            total_budget_b=total_budget_b,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/retrospective/latest")
def api_retrospective_latest():
    from services.storage import read_json
    return read_json("ledgers/ai_proposals/latest.json", default={"status": "no_proposals_yet"})


@router.post("/retrospective/deep-review")
def api_retrospective_deep_review(
    lookback_days: int = 45,
    step_days: int = 7,
    total_budget_b: float = 80.0,
    model: str = "auto",
    github_url: str | None = None,
    auto_pr: bool = True,
    auto_merge: bool = True,
    min_confidence: float = 0.75,
    min_uplift_pct: float = 10.0,
):
    from fastapi import HTTPException
    from services.retrospective import create_and_merge_github_pr, run_hybrid_code_review
    try:
        result = run_hybrid_code_review(
            lookback_days=lookback_days,
            step_days=step_days,
            total_budget_b=total_budget_b,
            model=model,
            github_url=github_url,
        )
        if auto_pr and "error" not in result:
            result["github_pr"] = create_and_merge_github_pr(
                result,
                auto_merge=auto_merge,
                base_branch="ai-autopilot",
                min_confidence=min_confidence,
                min_uplift_pct=min_uplift_pct,
            )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/retrospective/deep-review/latest")
def api_retrospective_deep_review_latest():
    from services.storage import read_json
    return read_json("ledgers/ai_proposals/latest_deep_review.json", default={"status": "no_deep_reviews_yet"})
