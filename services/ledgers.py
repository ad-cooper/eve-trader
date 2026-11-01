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
    if os.path.exists(SNIPER_DEALS_FILE):
        try:
            with open(SNIPER_DEALS_FILE, "r") as f:
                return json.load(f)
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
            qty = int(item.get("qty", 1))
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

            else:  # hold
                is_selling = (tid in sells)
                if is_selling and live_ask and live_ask > csv_sell:
                    t_sell = live_ask - 0.01
                else:
                    t_sell = csv_sell

                # Live Buy Safety: Must be competitive against live top buy, but NEVER exceed live ask!
                t_buy = csv_buy
                if live_top_buy and live_top_buy > 0:
                    t_buy = min(live_top_buy + 0.01, t_buy)

                # Hard ceiling: Hold buy orders must sit at least 10% BELOW live ask to capture a true dump
                if live_ask and live_ask > 0:
                    t_buy = min(t_buy, live_ask * 0.88)

                net_proceeds = t_sell * (1.0 - TOTAL_EXIT_FEES)

            item["target_sell"] = round(t_sell, 2)
            item["target_buy"] = round(t_buy, 2)

            cost_unit = t_buy * (1.0 + BUY_FEE_CITADEL)
            profit_unit = net_proceeds - cost_unit

            item["capital"] = round(cost_unit * qty, 2)
            item["profit"] = round(profit_unit * qty, 2)
            item["net_margin"] = round((profit_unit / cost_unit) * 100.0, 1) if cost_unit > 0 else 0.0

            # Proximity against Target Sell (with Stockout Fallback)
            if live_ask and t_sell > 0:
                item["proximity_pct"] = round((live_ask / t_sell) * 100.0, 1)
                item["diff_pct"] = round(((live_ask - t_sell) / t_sell) * 100.0, 1)
            else:
                # If no live ask in Jita 4-4, evaluate proximity against baseline CSV exit
                item["proximity_pct"] = round((csv_sell / t_sell) * 100.0, 1) if t_sell > 0 else None
                item["diff_pct"] = 0.0

            if tid in buys:
                b = buys[tid]
                item["char_status"] = "BUY_ACTIVE"
                item["my_price"] = b["price"]
                item["my_qty"] = b["volume_remain"]
                item["price_synced"] = abs(b["price"] - t_buy) / t_buy < 0.01 if t_buy > 0 else True
            elif tid in sells:
                s = sells[tid]
                item["char_status"] = "SELL_ACTIVE"
                item["my_price"] = s["price"]
                item["my_qty"] = s["volume_remain"]
                item["price_synced"] = True
            else:
                item["char_status"] = "UNPLACED"
                item["my_price"] = None
                item["my_qty"] = 0
                item["price_synced"] = False

        # Filter out peaked unplaced items from the Hold Desk
        if desk_type == "hold":
            clean_items = []
            for it in items:
                # If we own it (SELL_ACTIVE or in orders), keep it so we can liquidate at peak
                if it.get("char_status") != "UNPLACED":
                    clean_items.append(it)
                # If unplaced, only keep it if it is a genuine dip (live ask < target sell)
                elif it.get("proximity_pct") is None or it.get("proximity_pct") < 99.0:
                    clean_items.append(it)
            return clean_items

        return items

    return annotate(evergreen, "evergreen"), annotate(tactical, "tactical"), annotate(hold, "hold")
