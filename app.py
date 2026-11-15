#!/usr/bin/env python3
"""
EVE Trading Desk - Modular FastAPI Hub
======================================
"""

import os
import json
import subprocess
import threading
from datetime import datetime, timezone
from pydantic import BaseModel
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from services.esi import fetch_wallet_balance, get_auth_url, exchange_auth_code, load_sso_tokens, fetch_character_orders
from services.accounting import calculate_performance, load_performance
from services.ledgers import get_annotated_desks, load_sniper_deals, load_cached_orders
from services.market import update_ledger_market_prices

scheduler = AsyncIOScheduler(timezone="UTC")

STATE = {
    "sniper_running": False,
    "ledger_running": False,
    "last_sniper_run": None,
    "last_ledger_run": None,
}

def get_last_sniper_run_time():
    if STATE.get("last_sniper_run"):
        return STATE["last_sniper_run"]
    target = "ledgers/sniper_deals.json"
    if os.path.exists(target):
        try:
            mtime = os.path.getmtime(target)
            return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            pass
    return None

def get_last_ledger_run_time():
    for fpath in ["ledgers/evergreen_ledger.csv", "ledgers/hold_ledger.csv"]:
        if os.path.exists(fpath):
            try:
                mtime = os.path.getmtime(fpath)
                return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                pass
    return STATE.get("last_ledger_run")



def run_ledger_subprocess():
    STATE["ledger_running"] = True
    try:
        subprocess.run(["python3", "scripts/generate_ledgers.py", "--desk", "all", "--capital", "auto"], check=True)
        STATE["last_ledger_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        update_ledger_market_prices()
    finally:
        STATE["ledger_running"] = False

def run_sniper_subprocess():
    STATE["sniper_running"] = True
    try:
        res = subprocess.run(["python3", "scripts/scan_live_deals.py", "--mode", "all"], capture_output=True, text=True)
        raw_log = (res.stdout or "")
        if res.stderr:
            raw_log += f"\n\n=== STDERR / ERRORS ===\n{res.stderr}"
        STATE["last_sniper_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open("ledgers/live_sniper_cache.json", "w") as f:
            json.dump({"timestamp": get_last_sniper_run_time(), "raw_output": raw_log}, f)
        print(f"[+] Deal sniper completed (Exit Code: {res.returncode})")
        if res.stderr:
            print(f"[!] Deal sniper stderr:\n{res.stderr}")
    except Exception as e:
        print(f"[!] Error running deal sniper: {str(e)}")
    finally:
        STATE["sniper_running"] = False

def refresh_import_opportunities_job():
    try:
        from services.importer import get_import_suggestions
        get_import_suggestions()
    except Exception as e:
        print(f"[!] Background import scan error: {e}")

def _initial_startup_warmup():
    for fn in (fetch_character_orders, calculate_performance, update_ledger_market_prices):
        try:
            fn()
        except Exception as e:
            print(f"[!] Startup warmup error in {fn.__name__}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("PYTEST_CURRENT_TEST"):
        threading.Thread(target=_initial_startup_warmup, daemon=True).start()
        scheduler.add_job(run_ledger_subprocess, trigger="cron", hour=11, minute=30, id="daily_ledgers")
        scheduler.add_job(fetch_character_orders, trigger="interval", minutes=5, id="poll_orders")
        scheduler.add_job(calculate_performance, trigger="interval", minutes=15, id="calc_perf")
        scheduler.add_job(update_ledger_market_prices, trigger="interval", minutes=3, id="poll_market_prices")
        scheduler.add_job(refresh_import_opportunities_job, trigger="interval", minutes=30, id="poll_import_opportunities", max_instances=1, coalesce=True)
        scheduler.start()

        def _warmup_imports_if_stale():
            import time
            cache_path = "ledgers/import_suggestions_cache.json"
            if not os.path.exists(cache_path) or (time.time() - os.path.getmtime(cache_path)) > 1800:
                refresh_import_opportunities_job()

        threading.Thread(target=_warmup_imports_if_stale, daemon=True).start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="EVE Trading Desk", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/sso/login")
async def sso_login(role: str = "primary"):
    return RedirectResponse(get_auth_url(state=role or "primary"))


@app.get("/sso/callback")
async def sso_callback(code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(f"<h3>CCP SSO Error: {error}</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Error: No authorization code received</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)

    role = state or "primary"
    bundle = exchange_auth_code(code, role=role)
    if role == "cj6mt":
        if not bundle:
            return HTMLResponse(
                "<h3>C-J6MT Seller SSO Exchange Failed</h3>"
                "<p>Ensure <code>esi-markets.structure_markets.v1</code>, <code>esi-assets.read_assets.v1</code>, and <code>esi-markets.read_character_orders.v1</code> are enabled.</p>"
                "<p><a href='/'>Back to Dashboard</a></p>",
                status_code=400,
            )
        threading.Thread(target=refresh_import_opportunities_job, daemon=True).start()
        return RedirectResponse("/?tab=imports", status_code=303)
    if not bundle:
        return HTMLResponse(
            "<h3>SSO Token Exchange Failed</h3>"
            "<p>Check <code>docker compose logs</code> to see the exact rejection message from CCP.</p>"
            "<p><a href='/'>Back to Dashboard</a></p>",
            status_code=400,
        )
    fetch_character_orders()
    calculate_performance()
    return RedirectResponse("/", status_code=303)







@app.get("/api/state")
def api_state():
    eg, tac, hld = get_annotated_desks()
    orders = load_cached_orders()
    sso = load_sso_tokens() or {}

    def desk_metrics(desk_rows):
        cap = sum(r.get("capital", 0) for r in desk_rows)
        prof = sum(r.get("profit", 0) for r in desk_rows)
        return {"cap": cap, "prof": prof, "roi": (prof / cap * 100) if cap > 0 else 0.0}

    metrics_eg = desk_metrics(eg)
    metrics_tac = desk_metrics(tac)
    metrics_hld = desk_metrics(hld)

    total_cap = metrics_eg["cap"] + metrics_tac["cap"] + metrics_hld["cap"]
    total_prof = metrics_eg["prof"] + metrics_tac["prof"] + metrics_hld["prof"]
    all_rows = eg + tac + hld
    covered = sum(1 for r in all_rows if r.get("char_status") in ["BUY_ACTIVE", "SELL_ACTIVE"])

    sniper_cache = {}
    if os.path.exists("ledgers/live_sniper_cache.json"):
        try:
            with open("ledgers/live_sniper_cache.json", "r") as f:
                sniper_cache = json.load(f)
        except Exception:
            pass

    wallet_balance = fetch_wallet_balance() if "fetch_wallet_balance" in globals() else 0.0
    active_escrow = orders.get("total_escrow", 0.0)

    return JSONResponse({
        "status": {**STATE, "last_ledger_run": get_last_ledger_run_time()},
        "character": {
            "name": sso.get("character_name", "Hippie Father"),
            "character_id": sso.get("character_id"),
            "is_linked": bool(sso.get("character_id")),
            "orders_count": orders.get("count", 0),
            "total_escrow": active_escrow,
            "ledger_coverage": f"{covered} / {len(all_rows)}"
        },
        "metrics": {
            "total_capital": total_cap,
            "total_profit": total_prof,
            "blended_roi": (total_prof / total_cap * 100) if total_cap > 0 else 0,
            "liquid_reserve": wallet_balance,
            "portfolio_budget": (wallet_balance + active_escrow) if wallet_balance > 0 else total_cap,
            "order_slots": len(all_rows)
        },
        "desks": {"evergreen": eg, "tactical": tac, "hold": hld},
        "desk_metrics": {"evergreen": metrics_eg, "tactical": metrics_tac, "hold": metrics_hld},
        "sniper": {
            "timestamp": get_last_sniper_run_time(),
            "deals": load_sniper_deals(),
            "raw_output": sniper_cache.get("raw_output", "")
        },
        "performance": load_performance()
    })


@app.post("/api/orders/sync-esi")
async def sync_esi():
    fetch_character_orders()
    calculate_performance()
    return {"status": "success"}

@app.post("/api/market/refresh-prices")
async def refresh_market_prices(bg: BackgroundTasks):
    bg.add_task(update_ledger_market_prices)
    return {"status": "started"}

@app.get("/api/orders/missing-buys")
def api_missing_buys(desk: str = "all"):
    from services.ledgers import get_missing_buy_orders
    return get_missing_buy_orders(desk=desk)


@app.api_route("/api/inventory/advisor", methods=["GET", "POST"])
async def api_inventory_advisor(request: Request):
    from services.market import get_live_multisell_prices
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
    return get_live_multisell_prices(raw_text=raw_text, force_refresh=force_refresh)


@app.get("/api/import/opportunities")
def api_import_opportunities(
    amarr_warps: int = 35,
    cj6mt_rate: float = 800.0,
    limit: int = 100,
    force: bool = False,
):
    from services.importer import get_import_suggestions, load_cached_import_suggestions
    if not force:
        cached = load_cached_import_suggestions()
        if cached and cached.get("amarr", {}).get("pricing_model") == "hisec_tiered" and cached.get("cj6mt"):
            return cached
    return get_import_suggestions(amarr_warps=amarr_warps, cj6mt_rate=cj6mt_rate, limit=limit)



@app.get("/api/import/cj6mt-stock")
def api_import_cj6mt_stock():
    from services.importer import check_cj6mt_seller_stock
    return check_cj6mt_seller_stock()

@app.post("/api/import/bundles")
def api_import_bundles(payload: dict):
    from services.importer import suggest_courier_bundles
    return suggest_courier_bundles(
        dest_key=payload.get("dest_key", "amarr"),
        items=payload.get("items"),
        raw_text=payload.get("raw_text"),
        from_jita_hangar=bool(payload.get("from_jita_hangar", False)),
        rate_override=payload.get("rate_override"),
    )


@app.post("/api/ledgers/run")
def api_ledgers_run():
    if not STATE.get("ledger_running", False):
        threading.Thread(target=run_ledger_subprocess, daemon=True).start()
        return {"status": "started", "ledger_running": True}
    return {"status": "already_running", "ledger_running": True}

@app.post("/api/sniper/run")
def api_sniper_run():
    if not STATE.get("sniper_running", False):
        t = threading.Thread(target=run_sniper_subprocess, daemon=True)
        t.start()
        return {"status": "started", "sniper_running": True}
    return {"status": "already_running", "sniper_running": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)



