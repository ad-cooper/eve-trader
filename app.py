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

@app.on_event("startup")
async def startup_event():
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


def _set_url_query_param(url: str, key: str, val: str) -> str:
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parts = list(urlparse(url))
    q = parse_qs(parts[4], keep_blank_values=True)
    q[key] = [val]
    parts[4] = urlencode(q, doseq=True)
    return urlunparse(parts)


def _exchange_secondary_sso_code(code: str, role: str = "cj6mt") -> dict | None:
    """Exchanges an EVE SSO auth code for a secondary character without overwriting primary character."""
    import base64, time, requests
    import services.esi as esi_mod

    sso_file = getattr(esi_mod, "SSO_TOKEN_FILE", "ledgers/sso_token.json")
    client_id = os.environ.get("EVE_CLIENT_ID") or getattr(esi_mod, "CLIENT_ID", "")
    client_secret = os.environ.get("EVE_CLIENT_SECRET") or getattr(esi_mod, "CLIENT_SECRET", "")

    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://login.eveonline.com/v2/oauth/token",
        data={"grant_type": "authorization_code", "code": code},
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "login.eveonline.com",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[!] Secondary SSO token exchange failed ({resp.status_code}): {resp.text}")
        return None

    tok = resp.json()
    claims = esi_mod.decode_jwt_payload(tok["access_token"])
    sub = claims.get("sub", "")
    char_id = int(sub.split(":")[-1]) if ":" in sub else 0
    char_name = claims.get("name", "Unknown")

    new_entry = {
        "character_id": char_id,
        "character_name": char_name,
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "token_type": tok.get("token_type", "Bearer"),
        "expires_at": time.time() + float(tok.get("expires_in", 1199)),
        "role": role,
    }

    bundle = []
    if os.path.exists(sso_file):
        try:
            with open(sso_file, "r", encoding="utf-8") as f:
                bundle = json.load(f)
        except Exception:
            bundle = []

    if isinstance(bundle, list):
        # Preserve index 0 (primary Jita character), replace any prior C-J6MT / Skie Ankora secondary entry
        primary = bundle[0] if bundle else None
        kept = [primary] if primary else []
        for item in bundle[1:]:
            if not isinstance(item, dict):
                continue
            cname = str(item.get("character_name") or "")
            if role == "cj6mt" and (
                item.get("role") == "cj6mt"
                or cname.lower() == "skie ankora"
                or cname.lower() == char_name.lower()
            ):
                continue
            kept.append(item)
        if primary and str(primary.get("character_name") or "").lower() == char_name.lower():
            kept[0] = {**primary, **new_entry, "role": "primary"}
        else:
            kept.append(new_entry)
        bundle = kept
    elif isinstance(bundle, dict):
        chars = bundle.setdefault("characters", {})
        if role == "cj6mt" and char_name != "Skie Ankora":
            chars.pop("Skie Ankora", None)
        chars[char_name] = new_entry
        if role == "cj6mt":
            bundle["cj6mt_character"] = char_name
    else:
        bundle = [new_entry]

    os.makedirs(os.path.dirname(sso_file) or ".", exist_ok=True)
    with open(sso_file, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)

    print(f"[+] Linked C-J6MT seller character: {char_name} (ID: {char_id})")
    return new_entry



@app.get("/sso/login")
async def sso_login(role: str = "primary"):
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    url = get_auth_url()
    parts = list(urlparse(url))
    q = parse_qs(parts[4], keep_blank_values=True)
    existing = q.get("scope", [""])[0].split()
    for s in [
        "esi-wallet.read_character_wallet.v1",
        "esi-assets.read_assets.v1",
        "esi-markets.read_character_orders.v1",
        "esi-markets.structure_markets.v1",
        "esi-universe.read_structures.v1",
    ]:
        if s not in existing:
            existing.append(s)
    q["scope"] = [" ".join(existing)]
    if role and role != "primary":
        q["state"] = [role]
    parts[4] = urlencode(q, doseq=True)
    return RedirectResponse(urlunparse(parts))


@app.get("/sso/callback")
async def sso_callback(code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(f"<h3>CCP SSO Error: {error}</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Error: No authorization code received</h3><p><a href='/'>Back to Dashboard</a></p>", status_code=400)

    if state == "cj6mt":
        sec = _exchange_secondary_sso_code(code, role="cj6mt")
        if not sec:
            return HTMLResponse(
                "<h3>C-J6MT Seller SSO Exchange Failed</h3>"
                "<p>Ensure <code>esi-markets.structure_markets.v1</code>, <code>esi-assets.read_assets.v1</code>, and <code>esi-markets.read_character_orders.v1</code> are enabled.</p>"
                "<p><a href='/'>Back to Dashboard</a></p>",
                status_code=400,
            )
        threading.Thread(target=refresh_import_opportunities_job, daemon=True).start()
        return RedirectResponse("/?tab=imports", status_code=303)

    bundle = exchange_auth_code(code)
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
    import math, requests
    from concurrent.futures import ThreadPoolExecutor, as_completed

    eg, tac, hld = get_annotated_desks()
    desk_map = {
        "evergreen": eg,
        "tactical": tac,
        "hold": hld,
        "all": eg + tac + hld,
    }
    rows = desk_map.get(desk.lower(), eg + tac + hld)
    unplaced = [r for r in rows if r.get("char_status") == "UNPLACED"]
    if not unplaced:
        return {"total_items": 0, "total_escrow": 0.0, "items": [], "multibuy_text": ""}

    def eve_tick(p: float) -> float:
        if p <= 0:
            return 0.01
        mag = 10 ** math.floor(math.log10(p))
        return max(0.01, mag / 1000.0)

    def round_up_eve(p: float) -> float:
        t = eve_tick(p)
        return round(math.ceil(p / t) * t, 2)

    def round_down_eve(p: float) -> float:
        t = eve_tick(p)
        return round(math.floor(p / t) * t, 2)

    def fetch_jita_book(tid: int):
        try:
            r = requests.get(
                f"https://esi.evetech.net/latest/markets/10000002/orders/?datasource=tranquility&order_type=all&type_id={tid}",
                timeout=8,
            )
            if r.status_code != 200:
                return tid, 0.0, 0.0
            orders = r.json()
            jita_buys = [o["price"] for o in orders if o.get("location_id") == 60003760 and o.get("is_buy_order")]
            jita_sells = [o["price"] for o in orders if o.get("location_id") == 60003760 and not o.get("is_buy_order")]
            return tid, (max(jita_buys) if jita_buys else 0.0), (min(jita_sells) if jita_sells else 0.0)
        except Exception:
            return tid, 0.0, 0.0

    tids = list({int(r["type_id"]) for r in unplaced if r.get("type_id")})
    books = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tid, top_b, low_s in ex.map(fetch_jita_book, tids):
            books[tid] = (top_b, low_s)

    BUY_BROKER = 0.005
    SELL_TOTAL = 0.0462
    MIN_MARGIN = 5.0

    out_items = []
    for r in unplaced:
        tid = int(r["type_id"])
        qty = int(r.get("qty") or 1)
        target_buy = float(r.get("target_buy") or 0.0)
        target_sell = float(r.get("target_sell") or 0.0)
        top_buy, live_ask = books.get(tid, (0.0, 0.0))

        eff_sell = live_ask if live_ask > 0 else target_sell
        if eff_sell <= 0:
            continue

        max_buy_5pct = round_down_eve((eff_sell * (1.0 - SELL_TOTAL)) / ((1.0 + MIN_MARGIN / 100.0) * (1.0 + BUY_BROKER)))
        if max_buy_5pct <= 0:
            continue

        desired_buy = round_up_eve(top_buy + eve_tick(top_buy)) if top_buy > 0 else target_buy
        desired_buy = max(desired_buy, target_buy)

        if desired_buy <= max_buy_5pct:
            rec_buy = desired_buy
            squeezed = False
        else:
            rec_buy = max(target_buy, max_buy_5pct) if target_buy <= eff_sell * 0.90 else max_buy_5pct
            squeezed = True

        unit_cost = rec_buy * (1.0 + BUY_BROKER)
        unit_rev = eff_sell * (1.0 - SELL_TOTAL)
        margin_pct = round(((unit_rev - unit_cost) / unit_cost) * 100.0, 1) if unit_cost > 0 else 0.0
        line_escrow = round(rec_buy * qty, 2)

        out_items.append({
            "type_id": tid,
            "name": r.get("name"),
            "qty": qty,
            "top_buy": round(top_buy, 2),
            "recommended_buy": round(rec_buy, 2),
            "margin_pct": margin_pct,
            "line_escrow": line_escrow,
            "squeezed": squeezed,
        })

    out_items.sort(key=lambda x: (x["squeezed"], -x["line_escrow"]))
    multibuy_text = "\n".join(f"{it['name']} {it['qty']}" for it in out_items)
    total_escrow = round(sum(it["line_escrow"] for it in out_items), 2)

    return {
        "total_items": len(out_items),
        "total_escrow": total_escrow,
        "items": out_items,
        "multibuy_text": multibuy_text,
    }


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



