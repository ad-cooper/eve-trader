#!/usr/bin/env python3
"""
Temporary Blacklist Management Tool
===================================
Add, list, update, and remove items or groups from the temporary exclusion registry.
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta

BLACKLIST_FILE = "data/temporary_blacklist.json"

def load_data():
    if not os.path.exists(BLACKLIST_FILE):
        return {}
    with open(BLACKLIST_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

def cmd_list(args):
    data = load_data()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("\n" + "=" * 125)
    print(f"--- ACTIVE TEMPORARY BLACKLIST ({len(data)} ENTRIES REGISTERED) ---")
    print("=" * 125)
    print(f"{'NAME':<30} | {'TYPE':<6} | {'STATUS':<9} | {'EXPIRES':<10} | {'CATEGORY':<14} | {'REASON / NOTES'}")
    print("=" * 125)

    for name, entry in sorted(data.items()):
        is_active = entry.get("active", True)
        expires_at = entry.get("expires_at")
        
        status = "ACTIVE"
        if not is_active:
            status = "DISABLED"
        elif expires_at and expires_at < today_str:
            status = "EXPIRED"

        exp_str = expires_at if expires_at else "PERMANENT"
        cat_str = entry.get("category", "general")
        reason = entry.get("reason", "No reason provided")

        color_prefix = "\033[92m" if status == "ACTIVE" else "\033[90m"
        color_reset = "\033[0m"

        print(f"{name[:29]:<30} | {entry.get('type', 'any'):<6} | {color_prefix}{status:<9}{color_reset} | {exp_str:<10} | {cat_str:<14} | {reason[:40]}")
    print("=" * 125 + "\n")

def cmd_add(args):
    data = load_data()
    name = args.name.strip()
    today = datetime.now(timezone.utc)
    
    expires_at = None
    if args.days and args.days > 0:
        exp_dt = today + timedelta(days=args.days)
        expires_at = exp_dt.strftime("%Y-%m-%d")

    data[name] = {
        "type": args.type,
        "reason": args.reason,
        "category": args.category,
        "added_date": today.strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "active": True
    }
    save_data(data)
    print(f"[+] Blacklisted '{name}' (Type: {args.type}, Expires: {expires_at or 'Manual'}, Reason: {args.reason})")

def cmd_remove(args):
    data = load_data()
    name = args.name.strip()
    if name in data:
        del data[name]
        save_data(data)
        print(f"[+] Removed '{name}' from the blacklist.")
    else:
        print(f"[!] '{name}' was not found in the blacklist.")

def cmd_toggle(args):
    data = load_data()
    name = args.name.strip()
    if name in data:
        data[name]["active"] = not data[name].get("active", True)
        state = "ACTIVE" if data[name]["active"] else "DISABLED"
        save_data(data)
        print(f"[+] Toggled '{name}' to {state}.")
    else:
        print(f"[!] '{name}' was not found in the blacklist.")

def main():
    parser = argparse.ArgumentParser(description="EVE Trading Desk Temporary Blacklist Manager")
    subparsers = parser.add_subparsers(dest="command")

    # List command
    subparsers.add_parser("list", help="List all blacklisted items and notes")

    # Add command
    p_add = subparsers.add_parser("add", help="Add or update an item in the blacklist")
    p_add.add_argument("name", help="Item Name or Group Name")
    p_add.add_argument("--reason", required=True, help="Reason/Notes (e.g. Patch notes, meta shift)")
    p_add.add_argument("--type", choices=["item", "group", "any"], default="item", help="Exclusion level")
    p_add.add_argument("--category", default="patch_notes", choices=["patch_notes", "meta_shift", "market_trend", "logistics", "capital_cap"])
    p_add.add_argument("--days", type=int, default=None, help="Temporary duration in days (default: indefinite)")

    # Remove command
    p_rem = subparsers.add_parser("remove", help="Permanently remove an entry")
    p_rem.add_argument("name", help="Item or Group Name")

    # Toggle command
    p_tog = subparsers.add_parser("toggle", help="Toggle active status without deleting note")
    p_tog.add_argument("name", help="Item or Group Name")

    args = parser.parse_args()
    if args.command == "list" or not args.command:
        cmd_list(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "remove":
        cmd_remove(args)
    elif args.command == "toggle":
        cmd_toggle(args)

if __name__ == "__main__":
    main()
