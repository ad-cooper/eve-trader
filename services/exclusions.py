from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import pandas as pd

from constants import (
    BLACKLIST_FILE
)

# Shared catalog/universe exclusions across SQL and Python layers
EXCLUDED_MARKET_GROUP_IDS: tuple[int, ...] = (2487, 1661)
EXPIRED_DESCRIPTION_MARKERS: tuple[str, ...] = (
    "cease to function",
    "will expire",
    "expired",
)
EXCLUDED_CATEGORY_NAMES: tuple[str, ...] = (
    "Personalization",
    "SKINs",
    "Special Edition Assets",
    "Apparel",
)
EXCLUDED_NAME_REGEX: str = r"Modified|Voucher|Token|Certificate"

SNIPER_BANNED_NAME_SUBSTRINGS: tuple[str, ...] = (
    "' Modified ",
    "Mutaplasmid",
    "Accelerator",
    "Background",
    "Filament",
    "Blueprint",
    "Skin",
)

def matches_sniper_banned_name_substring(name: str) -> bool:
    """Returns True if `name` contains any configured banned item substring."""
    low = str(name or "").lower()
    return any(pat.lower() in low for pat in BANNED_NAME_SUBSTRINGS)

def sql_excluded_market_groups_clause(col: str = "t.marketGroupID") -> str:
    ids_csv = ", ".join(str(int(gid)) for gid in EXCLUDED_MARKET_GROUP_IDS)
    return f"COALESCE({col}, 0) NOT IN ({ids_csv})"


def sql_non_expired_description_clause(col: str = "t.description") -> str:
    clauses = [
        f"LOWER(COALESCE({col}, '')) NOT LIKE '%{marker.lower()}%'"
        for marker in EXPIRED_DESCRIPTION_MARKERS
    ]
    return " AND ".join(clauses)


def is_item_excluded(
    name: str,
    group_name: str,
    category_name: str = "",
    bl_items: set[str] | None = None,
    bl_groups: set[str] | None = None,
    bl_meta: dict | None = None,
    extra_categories: tuple[str, ...] = (),
    check_default_categories: bool = False,
    check_default_name_regex: bool = False,
) -> bool:
    """Checks temporary blacklist entries plus optional category/name exclusion rules."""
    if bl_items is None or bl_groups is None or bl_meta is None:
        loaded_items, loaded_groups, loaded_meta = load_active_blacklist()
        bl_items = loaded_items if bl_items is None else bl_items
        bl_groups = loaded_groups if bl_groups is None else bl_groups
        bl_meta = loaded_meta if bl_meta is None else bl_meta

    name_s = str(name or "")
    group_s = str(group_name or "")
    cat_s = str(category_name or "")

    if name_s in bl_items or group_s in bl_groups:
        return True

    for pat, meta in (bl_meta or {}).items():
        if str((meta or {}).get("type", "")).lower() == "pattern":
            try:
                if re.search(pat, name_s, flags=re.IGNORECASE):
                    return True
            except re.error:
                continue

    blocked_cats = set(extra_categories)
    if check_default_categories:
        blocked_cats.update(EXCLUDED_CATEGORY_NAMES)
    if blocked_cats and cat_s in blocked_cats:
        return True

    if check_default_name_regex and EXCLUDED_NAME_REGEX:
        if re.search(EXCLUDED_NAME_REGEX, name_s):
            return True

    return False


def build_static_exclusion_mask(
    df: pd.DataFrame,
    bl_items: set[str],
    bl_groups: set[str],
    bl_meta: dict,
    extra_excluded_categories: tuple[str, ...] = (),
) -> pd.Series:
    """Boolean mask (True = keep) for static blacklist, category, and name-pattern rules."""
    keep = pd.Series(True, index=df.index)

    if "group_name" in df.columns and bl_groups:
        keep &= ~df["group_name"].isin(bl_groups)
    if "item_name" in df.columns and bl_items:
        keep &= ~df["item_name"].isin(bl_items)

    bl_patterns = [
        pat for pat, meta in (bl_meta or {}).items()
        if str((meta or {}).get("type", "")).lower() == "pattern"
    ]
    if bl_patterns and "item_name" in df.columns:
        combined_pat = "|".join(f"(?:{p})" for p in bl_patterns)
        keep &= ~df["item_name"].str.contains(combined_pat, case=False, regex=True, na=False)

    blocked_cats = set(EXCLUDED_CATEGORY_NAMES) | set(extra_excluded_categories)
    if blocked_cats and "category_name" in df.columns:
        keep &= ~df["category_name"].isin(blocked_cats)

    if EXCLUDED_NAME_REGEX and "item_name" in df.columns:
        keep &= ~df["item_name"].str.contains(EXCLUDED_NAME_REGEX, regex=True, na=False)

    return keep

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
        elif target_type == "pattern":
            pass  # Evaluated as regex via active_meta without polluting exact-name sets
        else:  # "any"
            blacklisted_items.add(name)
            blacklisted_groups.add(name)

        active_meta[name] = entry

    return blacklisted_items, blacklisted_groups, active_meta
