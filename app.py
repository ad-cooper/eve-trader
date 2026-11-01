#!/usr/bin/env python3
"""
EVE Trading Desk - Modular FastAPI Hub
======================================
"""

import os
import json
import subprocess
from datetime import datetime, timezone
from pydantic import BaseModel
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from services.esi import get_auth_url, exchange_auth_code, load_sso_tokens, fetch_character_orders
from services.accounting import calculate_performance, load_performance
from services.ledgers import get_annotated_desks, load_sniper_deals, load_cached_orders
from services.market import update_ledger_market_prices

app = FastAPI(title="EVE Trading Desk")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler(timezone="UTC")

STATE = {
    "sniper_running": False,
    "ledger_running": False,
    "last_sniper_run": None,
    "last_ledger_run": None,
}

class MultiSellRequest(BaseModel):
    raw_text: Optional[str] = None

def run_ledger_subprocess():
    STATE["ledger_running"] = True
    try:
        subprocess.run(["python3", "scripts/generate_ledgers.py", "--desk", "all", "--capital", "50.0"], check=True)
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
            json.dump({"timestamp": STATE["last_sniper_run"], "raw_output": raw_log}, f)
        print(f"[+] Deal sniper completed (Exit Code: {res.returncode})")
        if res.stderr:
            print(f"[!] Deal sniper stderr:\n{res.stderr}")
    except Exception as e:
        print(f"[!] Error running deal sniper: {str(e)}")
    finally:
        STATE["sniper_running"] = False

@app.on_event("startup")
async def startup_event():
    fetch_character_orders()
    calculate_performance()
    update_ledger_market_prices()
    scheduler.add_job(run_ledger_subprocess, trigger="cron", hour=11, minute=30, id="daily_ledgers")
    scheduler.add_job(fetch_character_orders, trigger="interval", minutes=5, id="poll_orders")
    scheduler.add_job(calculate_performance, trigger="interval", minutes=15, id="calc_perf")
    scheduler.add_job(update_ledger_market_prices, trigger="interval", minutes=3, id="poll_market_prices")
    scheduler.start()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/sso/login")
async def sso_login():
    return RedirectResponse(get_auth_url())

@app.get("/sso/callback")
async def sso_callback(code: str = None, error: str = None):
    if error:
        return HTMLResponse(f"<h3>CCP SSO Error: {error}</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Error: No authorization code received</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)
    bundle = exchange_auth_code(code)
    if not bundle:
        return HTMLResponse(
            "<h3>SSO Token Exchange Failed</h3>"
            "<p>Check <code>docker compose logs</code> to see the exact rejection message from CCP.</p>"
            "<ul>"
            "<li>If HTTP 401: <code>EVE_CLIENT_SECRET</code> is incorrect in docker-compose.yml.</li>"
            "<li>If HTTP 400 invalid_scope: Ensure <code>esi-assets.read_assets.v1</code> is checked in CCP Developers Portal.</li>"
            "</ul>"
            "<p><a href='/'>Back to Dashboard</a></p>",
            status_code=400
        )
    fetch_character_orders()
    calculate_performance()
    return RedirectResponse("/", status_code=303)
    if code and exchange_auth_code(code):
        fetch_character_orders()
        calculate_performance()
    return RedirectResponse("/")

@app.get("/api/state")
async def api_state():
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

    return JSONResponse({
        "status": STATE,
        "character": {
            "name": sso.get("character_name", "Hippie Father"),
            "character_id": sso.get("character_id"),
            "is_linked": bool(sso.get("character_id")),
            "orders_count": orders.get("count", 0),
            "total_escrow": orders.get("total_escrow", 0.0),
            "ledger_coverage": f"{covered} / {len(all_rows)}"
        },
        "metrics": {
            "total_capital": total_cap,
            "total_profit": total_prof,
            "blended_roi": (total_prof / total_cap * 100) if total_cap > 0 else 0,
            "liquid_reserve": max(0.0, 50.0e9 - total_cap),
            "order_slots": len(all_rows)
        },
        "desks": {"evergreen": eg, "tactical": tac, "hold": hld},
        "desk_metrics": {"evergreen": metrics_eg, "tactical": metrics_tac, "hold": metrics_hld},
        "sniper": {
            "timestamp": STATE["last_sniper_run"],
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
async def api_missing_buys(desk: str = "all"):
    from services.market import get_live_missing_buys
    data = get_live_missing_buys(desk)
    return JSONResponse(data)

@app.post("/api/orders/multisell-price")
async def api_multisell_price(req: MultiSellRequest):
    from services.market import get_live_multisell_prices
    data = get_live_multisell_prices(req.raw_text)
    return JSONResponse(data)


from fastapi import Request

@app.api_route("/api/inventory/advisor", methods=["GET", "POST"])
async def api_inventory_advisor(request: Request):
    from services.market import get_live_multisell_prices
    raw_text = None
    if request.method == "POST":
        try:
            body = await request.json()
            raw_text = body.get("raw_text")
        except Exception:
            pass
    return get_live_multisell_prices(raw_text=raw_text)

@app.post("/api/market/multisell")
async def api_market_multisell_legacy(request: Request):
    return await api_inventory_advisor(request)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)



