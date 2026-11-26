import os
import json
import pandas as pd

from constants import (
    MARKET_CACHE_FILE
)

def read_json(path: str, default=None):
    """Safely reads a JSON file, returning `default` if missing or malformed."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, data, indent: int | None = 2) -> bool:
    """Writes JSON to `path`, creating parent directories if needed."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
        return True
    except Exception:
        return False


def read_csv_records(path: str) -> list[dict]:
    """Safely reads a CSV file as a list of row dicts, returning [] if missing or empty."""
    if not os.path.exists(path):
        return []
    try:
        return pd.read_csv(path).to_dict(orient="records")
    except Exception:
        return []




def load_market_prices_cache() -> dict:
    """Safely loads the cached live market prices JSON."""
    return read_json(MARKET_CACHE_FILE, default={})
