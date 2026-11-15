import os
import time
import json
import subprocess
import threading
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from services.esi import fetch_character_orders
from services.accounting import calculate_performance
from services.market import update_ledger_market_prices
from workflows.daily_pipeline import run_daily_ledger_rebuild
from workflows.imports import scan_import_opportunities

scheduler = AsyncIOScheduler(timezone="UTC")

STATE = {
    "sniper_running": False,
    "ledger_running": False,
    "last_sniper_run": None,
    "last_ledger_run": None,
}


def get_last_sniper_run_time() -> str | None:
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


def get_last_ledger_run_time() -> str | None:
    for fpath in ("ledgers/evergreen_ledger.csv", "ledgers/hold_ledger.csv"):
        if os.path.exists(fpath):
            try:
                mtime = os.path.getmtime(fpath)
                return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                pass
    return STATE.get("last_ledger_run")


def run_ledger_job() -> None:
    STATE["ledger_running"] = True
    try:
        success, ts = run_daily_ledger_rebuild()
        if success:
            STATE["last_ledger_run"] = ts
    finally:
        STATE["ledger_running"] = False


def run_sniper_job() -> None:
    STATE["sniper_running"] = True
    try:
        res = subprocess.run(["python3", "scripts/scan_live_deals.py", "--mode", "all"], capture_output=True, text=True)
        raw_log = res.stdout or ""
        if res.stderr:
            raw_log += f"\n\n=== STDERR / ERRORS ===\n{res.stderr}"
        STATE["last_sniper_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        os.makedirs("ledgers", exist_ok=True)
        with open("ledgers/live_sniper_cache.json", "w", encoding="utf-8") as f:
            json.dump({"timestamp": get_last_sniper_run_time(), "raw_output": raw_log}, f)
        print(f"[+] Deal sniper completed (Exit Code: {res.returncode})")
        if res.stderr:
            print(f"[!] Deal sniper stderr:\n{res.stderr}")
    except Exception as e:
        print(f"[!] Error running deal sniper: {e}")
    finally:
        STATE["sniper_running"] = False


def refresh_import_opportunities_job() -> None:
    try:
        scan_import_opportunities()
    except Exception as e:
        print(f"[!] Background import scan error: {e}")


def trigger_ledger_rebuild() -> dict:
    if not STATE.get("ledger_running", False):
        threading.Thread(target=run_ledger_job, daemon=True).start()
        return {"status": "started", "ledger_running": True}
    return {"status": "already_running", "ledger_running": True}


def trigger_sniper_scan() -> dict:
    if not STATE.get("sniper_running", False):
        threading.Thread(target=run_sniper_job, daemon=True).start()
        return {"status": "started", "sniper_running": True}
    return {"status": "already_running", "sniper_running": True}


def _initial_startup_warmup() -> None:
    for fn in (fetch_character_orders, calculate_performance, update_ledger_market_prices):
        try:
            fn()
        except Exception as e:
            print(f"[!] Startup warmup error in {fn.__name__}: {e}")


def _warmup_imports_if_stale() -> None:
    cache_path = "ledgers/import_suggestions_cache.json"
    if not os.path.exists(cache_path) or (time.time() - os.path.getmtime(cache_path)) > 1800:
        refresh_import_opportunities_job()


def start_background_scheduler() -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    threading.Thread(target=_initial_startup_warmup, daemon=True).start()
    scheduler.add_job(run_ledger_job, trigger="cron", hour=11, minute=30, id="daily_ledgers")
    scheduler.add_job(fetch_character_orders, trigger="interval", minutes=5, id="poll_orders")
    scheduler.add_job(calculate_performance, trigger="interval", minutes=15, id="calc_perf")
    scheduler.add_job(update_ledger_market_prices, trigger="interval", minutes=3, id="poll_market_prices")
    scheduler.add_job(
        refresh_import_opportunities_job,
        trigger="interval",
        minutes=30,
        id="poll_import_opportunities",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    threading.Thread(target=_warmup_imports_if_stale, daemon=True).start()


def stop_background_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
