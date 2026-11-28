import math
from services.storage import read_json, write_json
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from services.esi import fetch_wallet_transactions
from services.sde import resolve_type_ids
from constants import (
    BUY_SCC_SURCHARGE,
    PERF_CACHE_FILE,
    SALES_TAX_RATE,
    SELL_BROKER_FEE,
    TOTAL_EXIT_FEE,
)

# --- EVE 4-Significant-Figure Tick Helpers ---
def eve_tick(price: float) -> float:
    """Returns the minimum EVE market price increment (4 significant digits) for `price`."""
    if price <= 0:
        return 0.01
    mag = 10 ** math.floor(math.log10(price))
    return max(0.01, mag / 1000.0)


def round_up_eve(price: float) -> float:
    t = eve_tick(price)
    return round(math.ceil(price / t) * t, 2)


def round_down_eve(price: float) -> float:
    t = eve_tick(price)
    return round(math.floor(price / t) * t, 2)


# --- Unit & Line Economics ---
def calc_unit_cost(buy_price: float, buy_fee: float = BUY_SCC_SURCHARGE) -> float:
    """Gross ISK cost to acquire 1 unit including broker/SCC surcharge."""
    return buy_price * (1.0 + buy_fee)


def calc_net_proceeds(sell_price: float, exit_fee: float = TOTAL_EXIT_FEE) -> float:
    """Net ISK realized after broker fee and sales tax exit costs."""
    return sell_price * (1.0 - exit_fee)

def load_recent_losing_typeids(min_loss_isk: float = 1_000_000.0) -> set[int]:
    payload = load_performance() or {}
    items = payload.get("items") or []
    loss_cutoff = -abs(float(min_loss_isk))

    losing_tids: set[int] = set()
    for item in items:
        try:
            tid = int(item["type_id"])
            profit = float(item.get("profit", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if profit < loss_cutoff:
            losing_tids.add(tid)
    return losing_tids

def calc_max_profitable_buy(
    sell_price: float,
    min_margin_pct: float,
    buy_fee: float = BUY_SCC_SURCHARGE,
    exit_fee: float = TOTAL_EXIT_FEE,
) -> float:
    """
    Computes highest bid price that still delivers target net margin:
    Target_Buy = Net_Proceeds / (1 + Margin) / (1 + Buy_Fee)
    """
    net_rev = calc_net_proceeds(sell_price, exit_fee=exit_fee)
    return net_rev / (1.0 + (min_margin_pct / 100.0)) / (1.0 + buy_fee)


def calc_projected_margin(
    buy_price: float,
    sell_price: float,
    buy_fee: float = BUY_SCC_SURCHARGE,
    exit_fee: float = TOTAL_EXIT_FEE,
) -> float:
    """Calculates net profit margin percentage between buy bid and sell exit."""
    cost = calc_unit_cost(buy_price, buy_fee=buy_fee)
    if cost <= 0:
        return 0.0
    net_rev = calc_net_proceeds(sell_price, exit_fee=exit_fee)
    return ((net_rev - cost) / cost) * 100.0


def calc_line_financials(
    buy_price: float,
    sell_price: float,
    qty: int = 1,
    buy_fee: float = BUY_SCC_SURCHARGE,
    exit_fee: float = TOTAL_EXIT_FEE,
) -> dict:
    """
    Consolidated financial metrics for a ledger or deal row.
    Returns unit cost, net proceeds, profit, total capital, total profit, and net margin %.
    """
    cost_unit = calc_unit_cost(buy_price, buy_fee=buy_fee)
    net_rev_unit = calc_net_proceeds(sell_price, exit_fee=exit_fee)
    profit_unit = net_rev_unit - cost_unit

    return {
        "cost_unit": round(cost_unit, 2),
        "net_proceeds_unit": round(net_rev_unit, 2),
        "profit_unit": round(profit_unit, 2),
        "capital": round(cost_unit * qty, 2),
        "profit": round(profit_unit * qty, 2),
        "net_margin": round((profit_unit / cost_unit) * 100.0, 1) if cost_unit > 0 else 0.0,
    }


def match_fifo_lots(
    events: list[dict],
    buy_fee: float = BUY_SCC_SURCHARGE,
    exit_fee: float = TOTAL_EXIT_FEE,
    unmatched_cost_ratio: float | None = 0.88,
) -> list[dict]:
    """
    Chronologically pairs SELL events against BUY lots per item key using FIFO queues.
    Each input event dict expects:
      - key: grouping identifier (e.g. type_id or item_name)
      - date: datetime
      - is_buy: bool
      - qty: int/float
      - price: float (raw unit price before fees)
      - meta: optional dict preserved onto matched closed-trade records
    """
    inventory: dict = defaultdict(deque)
    closed_lots: list[dict] = []

    for ev in events:
        key = ev["key"]
        qty = ev["qty"]
        unit_price = ev["price"]
        dt = ev["date"]
        meta = ev.get("meta") or {}

        if ev["is_buy"]:
            inventory[key].append({
                "qty": qty,
                "unit_cost": calc_unit_cost(unit_price, buy_fee=buy_fee),
                "raw_buy_price": unit_price,
                "date": dt,
                "meta": meta,
            })
            continue

        remain = qty
        if unmatched_cost_ratio is None and not inventory[key]:
            continue

        net_rev_unit = calc_net_proceeds(unit_price, exit_fee=exit_fee)

        while remain > 0 and inventory[key]:
            lot = inventory[key][0]
            matched = min(remain, lot["qty"])
            cost_basis = matched * lot["unit_cost"]
            gross_rev = matched * unit_price
            net_rev = matched * net_rev_unit
            hold_days = (
                max(0.0, (dt - lot["date"]).total_seconds() / 86400.0)
                if isinstance(dt, datetime) and isinstance(lot["date"], datetime)
                else 0.0
            )

            closed_lots.append({
                "key": key,
                "close_date": dt,
                "qty": matched,
                "buy_price": lot["raw_buy_price"],
                "sell_price": unit_price,
                "cost_basis": cost_basis,
                "gross_proceeds": gross_rev,
                "net_proceeds": net_rev,
                "net_profit": net_rev - cost_basis,
                "hold_days": round(hold_days, 1),
                "buy_meta": lot["meta"],
                "sell_meta": meta,
            })

            lot["qty"] -= matched
            remain -= matched
            if lot["qty"] <= 0:
                inventory[key].popleft()

        if remain > 0 and unmatched_cost_ratio is not None:
            est_unit_cost = unit_price * unmatched_cost_ratio
            cost_basis = remain * est_unit_cost
            gross_rev = remain * unit_price
            net_rev = remain * net_rev_unit
            closed_lots.append({
                "key": key,
                "close_date": dt,
                "qty": remain,
                "buy_price": est_unit_cost,
                "sell_price": unit_price,
                "cost_basis": cost_basis,
                "gross_proceeds": gross_rev,
                "net_proceeds": net_rev,
                "net_profit": net_rev - cost_basis,
                "hold_days": 0.0,
                "buy_meta": {},
                "sell_meta": meta,
            })

    return closed_lots

def calculate_performance():
    transactions = fetch_wallet_transactions()
    if not transactions:
        return None

    transactions.sort(key=lambda x: x["date"])
    type_ids = [tx["type_id"] for tx in transactions]
    name_map = resolve_type_ids(type_ids)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    purchases_30d = 0.0
    fifo_events = []
    for tx in transactions:
        tx_dt = datetime.strptime(tx["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        is_buy = bool(tx["is_buy"])
        qty = tx["quantity"]
        unit_price = tx["unit_price"]
        if is_buy and tx_dt >= cutoff:
            purchases_30d += qty * unit_price
        fifo_events.append({
            "key": tx["type_id"],
            "date": tx_dt,
            "is_buy": is_buy,
            "qty": qty,
            "price": unit_price,
        })

    closed_lots = match_fifo_lots(fifo_events, unmatched_cost_ratio=0.88)

    daily_profit = defaultdict(float)
    item_stats = defaultdict(lambda: {"type_id": 0, "name": "", "qty_sold": 0, "revenue": 0.0, "cost": 0.0, "profit": 0.0})
    income_30d, net_30d = 0.0, 0.0

    for lot in closed_lots:
        tx_dt = lot["close_date"]
        if tx_dt < cutoff:
            continue
        tid = lot["key"]
        day_str = tx_dt.strftime("%Y-%m-%d")
        daily_profit[day_str] += lot["net_profit"]
        income_30d += lot["gross_proceeds"]
        net_30d += lot["net_profit"]

        item_stats[tid]["type_id"] = tid
        item_stats[tid]["name"] = name_map.get(tid, f"Type {tid}")
        item_stats[tid]["qty_sold"] += lot["qty"]
        item_stats[tid]["revenue"] += lot["gross_proceeds"]
        item_stats[tid]["cost"] += lot["cost_basis"]
        item_stats[tid]["profit"] += lot["net_profit"]

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
            "blended_margin": (net_30d / income_30d * 100) if income_30d > 0 else 0.0,
        },
        "chart": {"labels": labels, "daily_profits": daily_vals, "running_totals": running_vals},
        "items": items,
        "last_sync": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    write_json(PERF_CACHE_FILE, perf)
    return perf


def load_performance():
    return read_json(PERF_CACHE_FILE, default={})
