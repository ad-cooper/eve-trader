import os
import json
import pandas as pd

from constants import (
    LEDGER_DIR,
    MARKET_CACHE_FILE,
    ORDERS_CACHE_FILE
)

def read_json(path: str, default=None):
    """Safely reads a JSON file, returning `default` if missing or malformed."""
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path: str, data, indent: int | None = 2) -> bool:
    """Writes JSON to `path`, creating parent directories if needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    return True

def read_csv_records(path: str) -> list[dict]:
    """Safely reads a CSV file as a list of row dicts, returning [] if missing or empty."""
    if not os.path.exists(path):
        return []
    return pd.read_csv(path).to_dict(orient="records")

def load_market_prices_cache() -> dict:
    """Safely loads the cached live market prices JSON."""
    return read_json(MARKET_CACHE_FILE, default={})

def load_sniper_deals() -> list:
    data = read_json("ledgers/sniper_deals.json", default=[])
    return data if isinstance(data, list) else data.get("deals", [])


def load_desk_csv(filename: str) -> list[dict]:
    return read_csv_records(os.path.join(LEDGER_DIR, filename))

def load_all_desk_ledgers() -> dict[str, list[dict]]:
    return {
        "evergreen": load_desk_csv("evergreen_ledger.csv"),
        "tactical": load_desk_csv("tactical_ledger.csv"),
        "hold": load_desk_csv("hold_ledger.csv"),
    }

def load_cached_orders() -> dict:
    return read_json(ORDERS_CACHE_FILE, default={"buys": {}, "sells": {}, "count": 0, "total_escrow": 0.0})

def get_desk_claimed_type_ids(include_active_orders: bool = True) -> set[int]:
    """Returns all type_ids currently managed by Jita desks (CSVs + active Jita orders)."""
    tids: set[int] = set()
    for fname in ("evergreen_ledger.csv", "tactical_ledger.csv", "hold_ledger.csv"):
        for row in load_desk_csv(fname):
            if row.get("type_id"):
                try:
                    tids.add(int(row["type_id"]))
                except (ValueError, TypeError):
                    pass
    if include_active_orders:
        orders = load_cached_orders() or {}
        for bucket in ("buys", "sells"):
            for k in (orders.get(bucket) or {}).keys():
                if str(k).isdigit():
                    tids.add(int(k))
    return tids