#!/usr/bin/env python3
"""
EVE Online Inventory Advisor (CLI)
==================================
Thin terminal renderer over `services.market.get_live_multisell_prices()`.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.market import get_live_multisell_prices


def fmt_m(val: float | None) -> str:
    if not val or val <= 0:
        return "STOCKOUT"
    return f"{val / 1e6:.1f}M" if val >= 1e6 else f"{val:,.0f}"


def main():
    print("[*] Evaluating hangar inventory, live Jita books & desk targets...")
    report = get_live_multisell_prices()
    managed = [it for it in report.get("items", []) if it.get("desk") != "UNMANAGED"]

    if not managed:
        print("[*] None of your current hangar items match historical ledger tracking.")
        return

    print("\n" + "=" * 135)
    print("--- INVENTORY SELL ADVISOR ---")
    print("=" * 135)
    print(f"{'ITEM NAME':<35} | {'DESK':<10} | {'QTY':<5} | {'TARGET SELL':<12} | {'LIVE ASK':<12} | {'ACTION':<25} | {'LIST PRICE'}")
    print("=" * 135)

    for it in managed:
        action = str(it.get("action") or "")
        color = "\033[93m" if "HOLD" in action else "\033[92m"
        print(
            f"{str(it['name'])[:34]:<35} | {str(it['desk']):<10} | {int(it['qty']):<5} | "
            f"{fmt_m(it.get('target_sell')):>12} | {fmt_m(it.get('lowest_ask')):>12} | "
            f"{color}{action:<25}\033[0m | {fmt_m(it.get('recommended_sell')):>10}"
        )

    print("=" * 135)
    print(f"Total Ready Value: {report['ready_value']/1e9:.2f}B ISK | Est. Net P&L: {report['total_profit']/1e6:+.1f}M ISK")


if __name__ == "__main__":
    main()
