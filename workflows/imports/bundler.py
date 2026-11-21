import json
import math
import os

from services.esi import (
    create_esi_session as create_session,
    fetch_character_locations_stock,
    resolve_primary_character,
    resolve_role_character,
)
from services.market import ensure_jita_asks
from services.sde import parse_eve_item_lines as _parse_eve_item_lines, resolve_sde_items
from workflows.imports.freight import calc_courier_freight, calc_hisec_single_contract
from workflows.imports.scanner import IMPORT_DESTINATIONS, load_cached_import_suggestions


def _resolve_items_for_bundling(
    items: list[dict] | None = None,
    raw_text: str | None = None,
    from_jita_hangar: bool = False,
    dest_key: str = "amarr",
) -> list[dict]:
    """
    Normalizes purchased items from explicit dicts, pasted EVE inventory/MultiBuy text,
    or Jita 4-4 hangar assets (`ledgers/character_assets.json`), resolving packaged m3 and Jita unit price.
    """
    raw_list: list[dict] = []

    if raw_text and raw_text.strip():
        raw_list.extend(_parse_eve_item_lines(raw_text))

    if items:
        for it in items:
            if int(it.get("qty", 0)) > 0:
                raw_list.append(dict(it))

    if from_jita_hangar and not raw_list:
        cached = load_cached_import_suggestions() or {}
        dest_items = (cached.get(dest_key) or {}).get("items") or []
        suggested_map = {int(x["type_id"]): x for x in dest_items if x.get("type_id")}

        if os.path.exists("ledgers/character_assets.json"):
            try:
                with open("ledgers/character_assets.json", "r", encoding="utf-8") as f:
                    assets = json.load(f)
                hangar_counts: dict[int, int] = {}
                for row in (assets if isinstance(assets, list) else []):
                    tid = row.get("type_id")
                    if tid is not None and int(tid) in suggested_map:
                        hangar_counts[int(tid)] = hangar_counts.get(int(tid), 0) + int(row.get("quantity", 1))
                for tid, qty in hangar_counts.items():
                    s_it = suggested_map[tid]
                    raw_list.append({
                        "type_id": tid,
                        "name": s_it["name"],
                        "qty": qty,
                        "unit_m3": float(s_it["m3"]),
                        "unit_price": float(s_it["jita_ask"]),
                    })
            except Exception as e:
                print(f"[!] Failed to read Jita hangar assets for bundling: {e}")

    if not raw_list:
        return []

    need_lookup = [it for it in raw_list if not it.get("type_id") or not it.get("unit_m3") or not it.get("unit_price")]
    sde_by_name: dict[str, dict] = {}
    sde_by_tid: dict[int, dict] = {}

    if need_lookup:
        sde_by_tid, sde_by_name = resolve_sde_items(
            names=[it["name"] for it in need_lookup if it.get("name")],
            type_ids=[int(it["type_id"]) for it in need_lookup if it.get("type_id")],
        )

    resolved: list[dict] = []
    missing_price_tids: list[int] = []
    for it in raw_list:
        qty = int(it.get("qty", 0))
        if qty <= 0:
            continue
        tid = int(it["type_id"]) if it.get("type_id") else None
        name = str(it.get("name") or "").strip()
        sde_rec = sde_by_tid.get(tid) if tid else sde_by_name.get(name.lower())
        if sde_rec:
            tid = sde_rec["type_id"]
            name = sde_rec["name"]
        unit_m3 = float(it.get("unit_m3") or (sde_rec["unit_m3"] if sde_rec else 1.0))
        unit_price = float(it.get("unit_price") or it.get("jita_ask") or 0.0)
        if unit_price <= 0 and tid:
            missing_price_tids.append(tid)
        resolved.append({
            "type_id": tid,
            "name": name or f"Type {tid}",
            "qty": qty,
            "unit_m3": unit_m3,
            "unit_price": unit_price,
        })

    if missing_price_tids:
        session = create_session()
        jita_asks: dict[int, float | None] = {}
        ensure_jita_asks(session, missing_price_tids, jita_asks)
        for it in resolved:
            if it["unit_price"] <= 0 and it.get("type_id"):
                it["unit_price"] = float(jita_asks.get(it["type_id"]) or 0.0)

    combined: dict[str, dict] = {}
    for it in resolved:
        key = it["name"]
        if key not in combined:
            combined[key] = dict(it)
        else:
            combined[key]["qty"] += it["qty"]

    return list(combined.values())


def _split_items_into_lots(items: list[dict], max_col_per_lot: float, max_m3_per_lot: float) -> list[dict]:
    """Splits large stacks into clean sub-lots so bin-packing can fill 1.5B / m3 caps tightly."""
    lots = []
    for it in items:
        qty = int(it["qty"])
        if qty <= 0:
            continue
        u_m3 = float(it["unit_m3"])
        u_p = float(it["unit_price"])

        if qty == 1:
            lot_size = 1
        else:
            by_col = max(1, int(300_000_000.0 // max(u_p, 1.0))) if not math.isinf(max_col_per_lot) else qty
            by_m3 = max(1, int(15_000.0 // max(u_m3, 0.01)))
            min_chunk = max(1, math.ceil(qty / 12))
            lot_size = max(min_chunk, min(by_col, by_m3))
            hard_col = max(1, int(max_col_per_lot // max(u_p, 1.0))) if not math.isinf(max_col_per_lot) else qty
            hard_m3 = max(1, int(max_m3_per_lot // max(u_m3, 0.01)))
            lot_size = min(lot_size, hard_col, hard_m3)

        rem = qty
        while rem > 0:
            take = min(rem, lot_size)
            lots.append({
                "type_id": it.get("type_id"),
                "name": it["name"],
                "qty": take,
                "unit_m3": u_m3,
                "unit_price": u_p,
                "m3": take * u_m3,
                "collateral": take * u_p,
            })
            rem -= take
    return lots


def _finalize_bundle_list(bins: list[dict]) -> list[dict]:
    bundles = []
    for i, b in enumerate(bins, 1):
        merged_items: dict[str, dict] = {}
        for l in b["lots"]:
            key = l["name"]
            if key not in merged_items:
                merged_items[key] = {
                    "type_id": l.get("type_id"),
                    "name": l["name"],
                    "qty": 0,
                    "m3": 0.0,
                    "collateral": 0.0,
                }
            merged_items[key]["qty"] += l["qty"]
            merged_items[key]["m3"] += l["m3"]
            merged_items[key]["collateral"] += l["collateral"]

        items_list = sorted(merged_items.values(), key=lambda x: x["collateral"], reverse=True)
        bundles.append({
            "bundle_num": i,
            "tier": b["tier"],
            "item_lines": len(items_list),
            "total_units": sum(x["qty"] for x in items_list),
            "total_m3": round(b["m3"], 1),
            "collateral": round(b["collateral"], 2),
            "reward": round(b["reward"], 2),
            "copy_text": "\n".join(f"{x['name']} {x['qty']}" for x in items_list),
            "items": [
                {**x, "m3": round(x["m3"], 1), "collateral": round(x["collateral"], 2)}
                for x in items_list
            ],
        })
    return bundles


def bundle_courier_contracts(
    dest_key: str = "amarr",
    items: list[dict] | None = None,
    raw_text: str | None = None,
    from_jita_hangar: bool = False,
    rate_override: float | None = None,
) -> dict:
    """
    Packs purchased Jita 4-4 items into minimum-cost courier contract bundles for either:
      - 'amarr': 35-warp highsec tiered rate card (Small/DST <=62.5k m3 & <=1.5B @ 52.5M vs Freighter <=950k m3 & <=1.5B @ 87.5M)
      - 'cj6mt': Jump Freighter per-m3 rate card (<=350,000 m3 per contract, 5M min / 280M cap)
    """
    dest = next((d for d in IMPORT_DESTINATIONS if d["key"] == dest_key.lower()), IMPORT_DESTINATIONS[0])
    resolved_items = _resolve_items_for_bundling(
        items=items,
        raw_text=raw_text,
        from_jita_hangar=from_jita_hangar,
        dest_key=dest["key"],
    )

    if not resolved_items:
        return {
            "destination": dest["name"],
            "dest_key": dest["key"],
            "contract_count": 0,
            "total_m3": 0.0,
            "total_collateral": 0.0,
            "total_reward": 0.0,
            "bundles": [],
        }

    is_hisec = dest.get("pricing_model") == "hisec_tiered"
    bins: list[dict] = []

    if is_hisec:
        warps = int(rate_override) if rate_override is not None else int(dest.get("warps", 35))
        lowsec_warps = int(dest.get("lowsec_warps", 0))
        lots = _split_items_into_lots(resolved_items, max_col_per_lot=1_500_000_000.0, max_m3_per_lot=950_000.0)
        sorted_lots = sorted(
            lots,
            key=lambda x: (x["m3"] > 12_500.0, max(x["m3"] / 62_500.0, x["collateral"] / 1_500_000_000.0)),
            reverse=True,
        )

        for lot in sorted_lots:
            best_idx = None
            best_delta = float("inf")
            best_headroom = -1.0

            for idx, b in enumerate(bins):
                new_m3 = b["m3"] + lot["m3"]
                new_col = b["collateral"] + lot["collateral"]
                _, new_cost = calc_hisec_single_contract(new_m3, new_col, warps, lowsec_warps)
                if math.isinf(new_cost):
                    continue
                delta = new_cost - b["reward"]
                rem_col = max(0.0, 1_500_000_000.0 - new_col)
                if delta < best_delta - 1.0 or (abs(delta - best_delta) <= 1.0 and rem_col < best_headroom):
                    best_delta = delta
                    best_idx = idx
                    best_headroom = rem_col

            tier_new, new_bin_cost = calc_hisec_single_contract(lot["m3"], lot["collateral"], warps, lowsec_warps)
            if best_idx is not None and best_delta < new_bin_cost:
                b = bins[best_idx]
                b["m3"] += lot["m3"]
                b["collateral"] += lot["collateral"]
                b["lots"].append(lot)
                b["tier"], b["reward"] = calc_hisec_single_contract(b["m3"], b["collateral"], warps, lowsec_warps)
            else:
                bins.append({
                    "m3": lot["m3"],
                    "collateral": lot["collateral"],
                    "lots": [lot],
                    "tier": tier_new,
                    "reward": new_bin_cost,
                })

        merged_any = True
        while merged_any and len(bins) > 1:
            merged_any = False
            best_pair = None
            best_savings = 0.0
            for i in range(len(bins)):
                for j in range(i + 1, len(bins)):
                    comb_m3 = bins[i]["m3"] + bins[j]["m3"]
                    comb_col = bins[i]["collateral"] + bins[j]["collateral"]
                    _, comb_cost = calc_hisec_single_contract(comb_m3, comb_col, warps, lowsec_warps)
                    savings = (bins[i]["reward"] + bins[j]["reward"]) - comb_cost
                    if savings > best_savings + 1.0:
                        best_savings = savings
                        best_pair = (i, j)
            if best_pair is not None:
                i, j = best_pair
                b1, b2 = bins[i], bins[j]
                comb_m3 = b1["m3"] + b2["m3"]
                comb_col = b1["collateral"] + b2["collateral"]
                tier, reward = calc_hisec_single_contract(comb_m3, comb_col, warps, lowsec_warps)
                bins[i] = {
                    "m3": comb_m3,
                    "collateral": comb_col,
                    "lots": b1["lots"] + b2["lots"],
                    "tier": tier,
                    "reward": reward,
                }
                bins.pop(j)
                merged_any = True
    else:
        rate = rate_override if rate_override is not None else dest["freight_rate_m3"]
        max_jf_m3 = 350_000.0
        lots = _split_items_into_lots(resolved_items, max_col_per_lot=float("inf"), max_m3_per_lot=max_jf_m3)
        sorted_lots = sorted(lots, key=lambda x: x["m3"], reverse=True)

        for lot in sorted_lots:
            best_idx = None
            best_rem = float("inf")
            for idx, b in enumerate(bins):
                if b["m3"] + lot["m3"] <= max_jf_m3:
                    rem = max_jf_m3 - (b["m3"] + lot["m3"])
                    if rem < best_rem:
                        best_rem = rem
                        best_idx = idx
            if best_idx is not None:
                b = bins[best_idx]
                b["m3"] += lot["m3"]
                b["collateral"] += lot["collateral"]
                b["lots"].append(lot)
                b["reward"] = calc_courier_freight(b["m3"], b["collateral"], dest, rate_override=rate, apply_min=True)
            else:
                reward = calc_courier_freight(lot["m3"], lot["collateral"], dest, rate_override=rate, apply_min=True)
                bins.append({
                    "m3": lot["m3"],
                    "collateral": lot["collateral"],
                    "lots": [lot],
                    "tier": "Jump Freighter",
                    "reward": reward,
                })

    bundles = _finalize_bundle_list(bins)
    return {
        "destination": dest["name"],
        "dest_key": dest["key"],
        "contract_count": len(bundles),
        "total_m3": round(sum(b["total_m3"] for b in bundles), 1),
        "total_collateral": round(sum(b["collateral"] for b in bundles), 2),
        "total_reward": round(sum(b["reward"] for b in bundles), 2),
        "bundles": bundles,
    }
