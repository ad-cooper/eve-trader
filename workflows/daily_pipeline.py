import sys
import subprocess
from datetime import datetime, timezone
from services.market import update_ledger_market_prices
from services.accounting import calculate_performance
from services.esi import fetch_character_orders
from workflows.imports import scan_import_opportunities


def run_daily_ledger_rebuild(include_imports: bool = True) -> tuple[bool, str | None]:
    """
    Executes the full daily trading pipeline:
      1. Sync latest Everef Forge market history into DuckDB
      2. Run the dynamic multi-desk waterfall (Hold -> Tactical -> Evergreen)
      3. Sync live ESI character orders & refresh Jita 4-4 order-book telemetry
      4. Recalculate 30-day realized FIFO performance
      5. Refresh cross-region import opportunities (C-J6MT & Amarr)
    """
    try:
        subprocess.run(["python3", "scripts/sync_market_data.py"], check=False)
        subprocess.run(["python3", "scripts/generate_ledgers.py", "--desk", "all", "--capital", "auto"], check=True)
        fetch_character_orders()
        update_ledger_market_prices()
        calculate_performance()
        if include_imports:
            try:
                scan_import_opportunities()
            except Exception as imp_err:
                print(f"[!] Daily import scan warning: {imp_err}")
        return True, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception as e:
        print(f"[!] Daily ledger rebuild failed: {e}")
        return False, None


if __name__ == "__main__":
    ok, ts = run_daily_ledger_rebuild(include_imports=True)
    if ok:
        print(f"[+] Daily pipeline completed successfully at {ts}")
        sys.exit(0)
    sys.exit(1)
