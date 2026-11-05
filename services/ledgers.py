from services.esi import get_14d_average_volume
import os
import json
import pandas as pd

LEDGER_DIR = "ledgers"
ORDERS_CACHE_FILE = "ledgers/character_orders.json"
SNIPER_DEALS_FILE = "ledgers/sniper_deals.json"
MARKET_CACHE_FILE = "ledgers/market_prices_cache.json"

BUY_FEE_CITADEL = 0.0050
TOTAL_EXIT_FEES = 0.04605

def load_desk_csv(filename: str):
    path = os.path.join(LEDGER_DIR, filename)
    if not os.path.exists(path):
        return []
    try:
        return pd.read_csv(path).to_dict(orient="records")
    except Exception:
        return []

def load_cached_orders():
    if os.path.exists(ORDERS_CACHE_FILE):
        try:
            with open(ORDERS_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"buys": {}, "sells": {}, "count": 0, "total_escrow": 0.0}

def load_sniper_deals():
    for fpath in ["ledgers/live_deals.json", "ledgers/sniper_deals.json", "data/live_deals.json"]:
        if os.path.exists(fpath):
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                    if data:
                        import time; from datetime import datetime, timezone; STATE = getattr(__import__("app", fromlist=["STATE"]), "STATE", {}); STATE["last_sniper_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"); return data
            except Exception:
                pass
    return []

def load_market_prices_cache():
    if os.path.exists(MARKET_CACHE_FILE):
        try:
            with open(MARKET_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_annotated_desks():
    evergreen = load_desk_csv("evergreen_ledger.csv")
    tactical = load_desk_csv("tactical_ledger.csv")
    hold = load_desk_csv("hold_ledger.csv")
    my_orders = load_cached_orders()
    market_prices = load_market_prices_cache()

    buys = my_orders.get("buys", {})
    sells = my_orders.get("sells", {})

    def annotate(items, desk_type):
        for item in items:
            tid = str(item.get("type_id", ""))
            csv_buy = float(item.get("target_buy", 0.0))
            csv_sell = float(item.get("target_sell", 0.0))

            p_info = market_prices.get(tid) or {}
            live_ask = p_info.get("lowest_ask")
            live_top_buy = p_info.get("top_buy")
            item["live_ask"] = live_ask

            # --- DESK SPECIFIC EXECUTION MODELS ---
            if desk_type == "evergreen":
                t_sell = min(live_ask - 0.01, csv_sell * 1.15) if (live_ask and live_ask > 0) else csv_sell
                net_proceeds = t_sell * (1.0 - TOTAL_EXIT_FEES)
                max_profitable_buy = net_proceeds / 1.050 / (1.0 + BUY_FEE_CITADEL)

                if live_top_buy and live_top_buy > 0:
                    t_buy = min(live_top_buy + 0.01, max_profitable_buy)
                else:
                    t_buy = min(csv_buy, max_profitable_buy)

                ceil_guard = (live_ask * 0.98) if live_ask else (t_sell * 0.98)
                t_buy = min(t_buy, ceil_guard)
                qty = int(item.get("qty", 1))

            elif desk_type == "tactical":
                t_sell = (0.50 * (live_ask - 0.01)) + (0.50 * csv_sell) if (live_ask and live_ask > 0) else csv_sell
                net_proceeds = t_sell * (1.0 - TOTAL_EXIT_FEES)
                max_profitable_buy = net_proceeds / 1.085 / (1.0 + BUY_FEE_CITADEL)
                if live_top_buy and live_top_buy > 0:
                    t_buy = min(live_top_buy + 0.01, max_profitable_buy)
                else:
                    t_buy = min(csv_buy, max_profitable_buy)
                ceil_guard = (live_ask * 0.95) if live_ask else (t_sell * 0.95)
                t_buy = min(t_buy, ceil_guard)
                qty = int(item.get("qty", 1))

            else:  # hold
                is_selling = (tid in sells)
                if is_selling and live_ask and live_ask > csv_sell:
                    t_sell = live_ask - 0.01
                else:
                    t_sell = csv_sell

                t_buy = csv_buy
                if live_top_buy and live_top_buy > 0:
                    t_buy = min(live_top_buy + 0.01, t_buy)
                if live_ask and live_ask > 0:
                    t_buy = min(t_buy, live_ask * 0.88)

                net_proceeds = t_sell * (1.0 - TOTAL_EXIT_FEES)

                # Preliminary sizing: Hold targets ~1.5B ISK per slot
                cost_unit = t_buy * (1.0 + BUY_FEE_CITADEL)
                if tid in buys or tid in sells:
                    qty = int(item.get("qty", 1))
                else:
                    qty = max(1, int(round(1_500_000_000.0 / cost_unit))) if cost_unit > 0 else 1

            item["target_sell"] = round(t_sell, 2)
            item["target_buy"] = round(t_buy, 2)

            cost_unit = t_buy * (1.0 + BUY_FEE_CITADEL)
            profit_unit = net_proceeds - cost_unit

            item["qty"] = qty
            item["capital"] = round(cost_unit * qty, 2)
            item["profit"] = round(profit_unit * qty, 2)
            item["net_margin"] = round((profit_unit / cost_unit) * 100.0, 1) if cost_unit > 0 else 0.0

            # Proximity against Target Sell
            if live_ask and t_sell > 0:
                item["proximity_pct"] = round((live_ask / t_sell) * 100.0, 1)
                item["diff_pct"] = round(((live_ask - t_sell) / t_sell) * 100.0, 1)
            else:
                item["proximity_pct"] = round((csv_sell / t_sell) * 100.0, 1) if t_sell > 0 else None
                item["diff_pct"] = 0.0

            # Order Status Tagging
            if tid in buys:
                b = buys[tid]
                item["char_status"] = "BUY_ACTIVE"
                item["my_price"] = b["price"]
                item["my_qty"] = b["volume_remain"]
                item["escrow"] = float(b.get("escrow", item.get("capital", 0.0)))
                item["price_synced"] = abs(b["price"] - t_buy) / t_buy < 0.01 if t_buy > 0 else True

                my_p = float(b.get("price", 0.0))
                top_b = float(live_top_buy or 0.0)

                if top_b > 0 and my_p >= (top_b - 0.01):
                    item["action_code"] = "TOP_BOOK"
                    item["action_text"] = "TOP BID (WINNING)"
                elif desk_type in ["hold", "tactical"]:
                    item["action_code"] = "PATIENT"
                    item["action_text"] = "HOLD FIRM (WAIT DUMP)"
                elif desk_type == "evergreen":
                    potential_cost = (top_b + 0.01) * (1.0 + BUY_FEE_CITADEL)
                    net_rev = t_sell * (1.0 - TOTAL_EXIT_FEES)
                    proj_margin = ((net_rev - potential_cost) / potential_cost) * 100.0 if potential_cost > 0 else 0.0
                    if top_b <= max_profitable_buy and proj_margin >= 4.5:
                        item["action_code"] = "BUMP_VIABLE"
                        item["action_text"] = f"BUMP VIABLE (+{proj_margin:.1f}%)"
                    else:
                        item["action_code"] = "PATIENT"
                        item["action_text"] = "CEILING REACHED (HOLD)"
                else:
                    item["action_code"] = "PATIENT"
                    item["action_text"] = "HOLD FIRM"

            elif tid in sells:
                s = sells[tid]
                item["char_status"] = "SELL_ACTIVE"
                item["my_price"] = s["price"]
                item["my_qty"] = s["volume_remain"]
                item["price_synced"] = True
                item["action_code"] = "SELL_ACTIVE"
                item["action_text"] = "SELL LISTED"

            else:
                item["char_status"] = "UNPLACED"
                item["escrow"] = 0.0
                item["my_price"] = None
                item["my_qty"] = 0
                item["price_synced"] = False

                if desk_type == "hold":
                    prox = item.get("proximity_pct")
                    margin = item.get("net_margin", 0.0)

                    # Only check ESI history on unplaced items inside the dip window
                    if prox is not None and 60.0 <= prox <= 92.0 and margin >= 20.0:
                        vol = get_14d_average_volume(int(tid)) if tid.isdigit() else -1.0
                        item["avg_daily_vol"] = vol

                        base_qty = max(1, int(round(1_500_000_000.0 / cost_unit))) if cost_unit > 0 else 1
                        if vol > 0:
                            # Cap quantity at 35% of 14-day average daily turnover
                            vol_cap = max(1, int(round(vol * 0.35)))
                            final_qty = min(base_qty, vol_cap)
                        else:
                            # Fallback: if ESI history is offline or zero, preserve CSV unit constraint
                            final_qty = min(base_qty, max(1, int(item.get("qty", 1))))

                        item["qty"] = final_qty
                        item["capital"] = round(cost_unit * final_qty, 2)
                        item["profit"] = round(profit_unit * final_qty, 2)

                        # Liquidity Trap Guard: filter out low-velocity items (e.g., Mimesis Epsilon)
                        if 0 <= vol < 2.5 and final_qty <= 1 and item["capital"] < 500_000_000.0:
                            item["action_code"] = "PATIENT"
                            item["action_text"] = "LOW VELOCITY"
                        else:
                            item["action_code"] = "PRIME_DIP"
                            item["action_text"] = "PRIME DIP"

                    elif prox is not None and prox < 60.0:
                        item["action_code"] = "PATIENT"
                        item["action_text"] = "DEEP CRASH"
                    elif prox is not None and prox > 92.0:
                        item["action_code"] = "PATIENT"
                        item["action_text"] = "NEAR PEAK"
                    else:
                        item["action_code"] = "UNPLACED"
                        item["action_text"] = "NEW ORDER"
                else:
                    item["action_code"] = "UNPLACED"
                    item["action_text"] = "READY TO PLACE"

        # --- HOLD DESK CURATION: KEEP ACTIVE ORDERS + TOP 8 PRIME DIPS ---
        if desk_type == "hold":
            active_items = [it for it in items if it.get("char_status") != "UNPLACED"]
            unplaced_prime = [
                it for it in items 
                if it.get("char_status") == "UNPLACED" and it.get("action_code") == "PRIME_DIP"
            ]
            unplaced_prime.sort(key=lambda x: -x.get("profit", 0.0))
            return active_items + unplaced_prime[:8]

        return items

    return annotate(evergreen, "evergreen"), annotate(tactical, "tactical"), annotate(hold, "hold")
