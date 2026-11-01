import json
import duckdb
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from services.esi import fetch_wallet_transactions

PERF_CACHE_FILE = "ledgers/performance_cache.json"
DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
SALES_TAX_RATE = 0.03375
SELL_BROKER_FEE = 0.0123
TOTAL_EXIT_FEE = SALES_TAX_RATE + SELL_BROKER_FEE  # 4.605%
BUY_SCC_SURCHARGE = 0.0050  # Citadel 1-jump surcharge

def get_item_names(type_ids: list) -> dict:
    if not type_ids:
        return {}
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
        id_str = ",".join(str(int(t)) for t in set(type_ids))
        res = con.execute(f"SELECT typeID, typeName FROM sde.invTypes WHERE typeID IN ({id_str})").fetchall()
        con.close()
        return {r[0]: r[1] for r in res}
    except Exception:
        return {}

def calculate_performance():
    transactions = fetch_wallet_transactions()
    if not transactions:
        return None

    transactions.sort(key=lambda x: x["date"])
    type_ids = [tx["type_id"] for tx in transactions]
    name_map = get_item_names(type_ids)

    inventory = defaultdict(deque)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    daily_profit = defaultdict(float)
    item_stats = defaultdict(lambda: {"type_id": 0, "name": "", "qty_sold": 0, "revenue": 0.0, "cost": 0.0, "profit": 0.0})

    income_30d, purchases_30d, net_30d = 0.0, 0.0, 0.0

    for tx in transactions:
        tid = tx["type_id"]
        qty = tx["quantity"]
        unit_price = tx["unit_price"]
        tx_dt = datetime.strptime(tx["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        day_str = tx_dt.strftime("%Y-%m-%d")

        if tx["is_buy"]:
            inventory[tid].append([qty, unit_price * (1.0 + BUY_SCC_SURCHARGE)])
            if tx_dt >= cutoff:
                purchases_30d += (qty * unit_price)
        else:
            net_revenue = (unit_price * (1.0 - TOTAL_EXIT_FEE)) * qty
            gross_revenue = unit_price * qty
            matched_cost = 0.0
            remain = qty

            while remain > 0 and inventory[tid]:
                lot = inventory[tid][0]
                if lot[0] <= remain:
                    matched_cost += (lot[0] * lot[1])
                    remain -= lot[0]
                    inventory[tid].popleft()
                else:
                    matched_cost += (remain * lot[1])
                    lot[0] -= remain
                    remain = 0

            if remain > 0:
                matched_cost += (remain * (unit_price * 0.88))

            profit = net_revenue - matched_cost

            if tx_dt >= cutoff:
                daily_profit[day_str] += profit
                income_30d += gross_revenue
                net_30d += profit

                item_stats[tid]["type_id"] = tid
                item_stats[tid]["name"] = name_map.get(tid, f"Type {tid}")
                item_stats[tid]["qty_sold"] += qty
                item_stats[tid]["revenue"] += gross_revenue
                item_stats[tid]["cost"] += matched_cost
                item_stats[tid]["profit"] += profit

    calendar = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    labels, daily_vals, running_vals = [], [], []
    cumulative = 0.0

    for day in calendar:
        labels.append(day[5:])
        p = daily_profit.get(day, 0.0)
        cumulative += p
        daily_vals.append(round(p, 2))
        running_vals.append(round(cumulative, 2))

    items = [it for it in item_stats.values() if abs(it["profit"]) >= 1.0e6 or it["revenue"] >= 5.0e6]
    items.sort(key=lambda x: x["profit"], reverse=True)

    perf = {
        "kpis": {
            "net_profit_30d": net_30d,
            "trade_income_30d": income_30d,
            "trade_purchases_30d": purchases_30d,
            "blended_margin": (net_30d / income_30d * 100) if income_30d > 0 else 0.0
        },
        "chart": {"labels": labels, "daily_profits": daily_vals, "running_totals": running_vals},
        "items": items,
        "last_sync": now.strftime("%Y-%m-%d %H:%M:%S UTC")
    }

    with open(PERF_CACHE_FILE, "w") as f:
        json.dump(perf, f, indent=2)
    return perf

def load_performance():
    try:
        with open(PERF_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None
