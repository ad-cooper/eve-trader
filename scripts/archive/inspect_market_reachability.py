#!/usr/bin/env python3
"""
Corrected Market Execution Flow Inspector
Uses Volume-Weighted Average Price (VWAP) positioning between live bid and ask
to measure actual buy-order fill volume vs. sell-order consumption.
"""

import time
import pandas as pd
import requests

REGION_THE_FORGE = 10000002
JITA_STATION_ID = 60003760

CANDIDATES = [
    {"type_id": 92514, "name": "Ultratidal Entropic Disintegrator II"},
    {"type_id": 57479, "name": "Core Temperature Regulator"},
    {"type_id": 56715, "name": "Cultural Semiosis Data"},
    {"type_id": 22110, "name": "Mid-grade Crystal Epsilon"},
    {"type_id": 33820, "name": "Barghest"},
    {"type_id": 21740, "name": "Caldari Navy Antimatter Charge L"},
    {"type_id": 54753, "name": "Large Vorton Projector II"},
    {"type_id": 33985, "name": "Prototype Hyperspatial Accelerator"},
    {"type_id": 14264, "name": "Domination Stasis Webifier"},
    {"type_id": 18973, "name": "Core X-Type EM Armor Hardener"},
]


def fetch_live_orders(type_id: int):
  url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}"
  headers = {"User-Agent": "EVE-VWAP-Inspector/2.0"}
  try:
    r = requests.get(url, headers=headers, timeout=12)
    if r.status_code == 200:
      orders = [o for o in r.json() if o.get("location_id") == JITA_STATION_ID]
      buys = [o["price"] for o in orders if o.get("is_buy_order")]
      sells = [o["price"] for o in orders if not o.get("is_buy_order")]
      if buys and sells:
        return max(buys), min(sells)
  except Exception:
    pass
  return None, None


def fetch_history(type_id: int):
  url = f"https://esi.evetech.net/latest/markets/{REGION_THE_FORGE}/history/?datasource=tranquility&type_id={type_id}"
  headers = {"User-Agent": "EVE-VWAP-Inspector/2.0"}
  try:
    r = requests.get(url, headers=headers, timeout=12)
    if r.status_code == 200:
      df = pd.DataFrame(r.json())
      df["date"] = pd.to_datetime(df["date"])
      return df.sort_values(by="date", ascending=False).head(30)
  except Exception:
    pass
  return pd.DataFrame()


def main():
  print("=" * 135)
  print(
      f"{'ITEM NAME':<38} | {'TOP BUY':<10} | {'MIN SELL':<10} | {'30D VWAP':<10}"
      f" | {'BUY-SIDE %':<11} | {'VERDICT'}"
  )
  print("=" * 135)

  for it in CANDIDATES:
    tid = it["type_id"]
    name = it["name"]

    top_buy, min_sell = fetch_live_orders(tid)
    hist_df = fetch_history(tid)

    if top_buy is None or min_sell is None or hist_df.empty:
      continue

    # Volume-weighted average price across trailing 30 days
    total_isk = (hist_df["average"] * hist_df["volume"]).sum()
    total_vol = hist_df["volume"].sum()
    if total_vol == 0:
      continue
    vwap = total_isk / total_vol

    # Calculate where VWAP sits between Top Buy and Min Sell
    # Buy-Side %: 100% means all volume executed at Top Buy, 0% means all at Min Sell
    spread = min_sell - top_buy
    if spread > 0:
      buy_side_ratio = ((min_sell - vwap) / spread) * 100.0
      buy_side_ratio = max(0.0, min(100.0, buy_side_ratio))
    else:
      buy_side_ratio = 0.0

    b_str = f"{top_buy/1e6:.2f}M" if top_buy >= 1e6 else f"{top_buy:,.0f}"
    s_str = f"{min_sell/1e6:.2f}M" if min_sell >= 1e6 else f"{min_sell:,.0f}"
    v_str = f"{vwap/1e6:.2f}M" if vwap >= 1e6 else f"{vwap:,.0f}"

    if buy_side_ratio < 25.0:
      verdict = "ONE-SIDED SELL TRAP (NO DUMPS)"
    elif buy_side_ratio >= 50.0:
      verdict = "HEALTHY: HEAVY TWO-SIDED DUMPS"
    else:
      verdict = "MODERATE TWO-SIDED FLOW"

    print(
        f"{name[:37]:<38} | {b_str:>10} | {s_str:>10} | {v_str:>10} |"
        f" {buy_side_ratio:>10.1f}% | {verdict}"
    )
    time.sleep(0.04)

  print("=" * 135)


if __name__ == "__main__":
  main()
