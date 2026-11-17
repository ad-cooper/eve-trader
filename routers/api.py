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
