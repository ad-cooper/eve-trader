import os
import json
from datetime import datetime, timezone

BLACKLIST_FILE = "data/temporary_blacklist.json"

def load_active_blacklist():
    """
    Returns (blacklisted_items: set, blacklisted_groups: set, metadata: dict)
    Filters out inactive items and items whose expires_at date has passed.
    """
    if not os.path.exists(BLACKLIST_FILE):
        return set(), set(), {}

    try:
        with open(BLACKLIST_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[!] Warning: Failed to parse {BLACKLIST_FILE}: {e}")
        return set(), set(), {}

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blacklisted_items = set()
    blacklisted_groups = set()
    active_meta = {}

    for name, entry in data.items():
        if not entry.get("active", True):
            continue

        expires_at = entry.get("expires_at")
        # Automatically skip if the temporary ban has expired
        if expires_at and expires_at < today_str:
            continue

        target_type = entry.get("type", "any").lower()
        if target_type == "group":
            blacklisted_groups.add(name)
        elif target_type == "item":
            blacklisted_items.add(name)
        else:  # "any"
            blacklisted_items.add(name)
            blacklisted_groups.add(name)

        active_meta[name] = entry

    return blacklisted_items, blacklisted_groups, active_meta
