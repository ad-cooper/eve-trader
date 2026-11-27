from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from services.exclusions import load_active_blacklist
from services.exclusions import (
    sql_excluded_market_groups_clause,
    sql_non_expired_description_clause,
)
from services.esi import (
    JITA_STATION_ID,
    create_esi_session as create_session,
    fetch_character_locations_stock,
    fetch_structure_order_book,
    get_30d_average_volume,
    resolve_primary_character,
    resolve_role_character,
)
from services.ledgers import get_desk_claimed_type_ids, is_item_blacklisted
from services.market import ensure_jita_asks, fetch_station_asks_bulk
from services.sde import PACKAGED_SHIP_M3, connect_analytics, resolve_sde_items
from services.staging_diff import get_observed_staging_velocity, process_snapshot_diff
from services.storage import read_json, write_json
from workflows.imports.freight import calc_courier_freight, calc_hisec_optimal_split

from constants import (
    IMPORT_CACHE_FILE
)

IMPORT_DESTINATIONS = [
    {
        "key": "amarr",
        "name": "AMARR",
        "type": "station",
        "pricing_model": "hisec_tiered",
        "location_id": 60008494,
        "region_id": 10000043,
        "warps": 35,
        "lowsec_warps": 0,
        "exit_fee": 0.0485,
        "min_margin_pct": 8.0,
        "min_isk_per_m3": 0.0,
        "min_jita_isk_day": 150_000_000.0,
        "min_jita_units_day": 8.0,
        "min_dest_units_day": 3.0,
        "volume_scale_factor": 1.0,
        "target_days_supply": 2.0,
        "candidate_limit": 250,
    },
    {
        "key": "cj6mt",
        "name": "C-J6MT",
        "type": "structure",
        "pricing_model": "per_m3",
        "location_id": 1049588174021,
        "character_name": "Skie Ankora",
        "freight_rate_m3": 800.0,
        "collateral_pct": 0.0,
        "min_freight": 5_000_000.0,
        "max_freight": 280_000_000.0,
        "exit_fee": 0.041,
        "min_margin_pct": 12.0,
        "max_markup_pct": 150.0,
        "min_isk_per_m3": 1500.0,
        "min_jita_isk_day": 350_000_000.0,
        "min_jita_units_day": 22.0,
        "min_dest_units_day": 1.0,
        "volume_scale_factor": 0.045,  # Prior: C-J6MT ~4.5% of Jita daily units; blends into observed burn over 7d
        "target_days_supply": 3.0,
        "candidate_limit": 350,
    },
]


ALLOWED_SDE_CATEGORIES = (
    "Ship",
    "Module",
    "Charge",
    "Drone",
    "Fighter",
    "Implant",
    "Deployable",
    "Subsystems",
    "Skill",
)


EXCLUDED_META_GROUPS = (5, 15)


EXCLUDED_TYPE_IDS = (44992,)


def _fetch_station_order_book(session, dest: dict, tids: list[int], jita_asks: dict[int, float | None]) -> dict:
    """Bulk-screens station asks via Fuzzwork, then validates 30d regional volume only on profitable candidates."""
    region_id = dest["region_id"]
    station_id = dest["location_id"]
    min_margin = dest["min_margin_pct"]
    min_daily_vol = dest.get("min_dest_units_day", 0.0)

    station_asks = fetch_station_asks_bulk(session, station_id, tids)
    profitable_tids = [
        tid for tid, dest_ask in station_asks.items()
        if jita_asks.get(tid) and dest_ask > jita_asks[tid] * (1.0 + min_margin / 100.0)
    ]

    def check_vol(tid: int):
        dest_ask = station_asks[tid]
        daily_vol = None
        if min_daily_vol > 0:
            daily_vol = get_30d_average_volume(tid, region_id=region_id)
            if daily_vol < min_daily_vol:
                return None

        return tid, {
            "lowest_ask": dest_ask,
            "sell_vol": 0,
            "top_buy": None,
            "daily_vol": daily_vol,
        }

    book = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for res in ex.map(check_vol, profitable_tids):
            if res:
                book[res[0]] = res[1]
    return book


def _get_jita_high_velocity_candidates(
    min_isk_day: float = 350_000_000.0,
    min_units_day: float = 22.0,
    limit: int = 300,
    extra_tids: list[int] | None = None,
) -> dict[int, dict]:
    """
    Selects high-velocity Jita player-consumed items clearing both ISK/day and units/day cutoffs,
    plus any extra_tids with proven local staging burn rates.
    """
    bl_items, bl_groups, bl_meta = load_active_blacklist()
    with connect_analytics(read_only=True, attach_sde=True) as con:
        allowed_cats = ",".join(f"'{c}'" for c in ALLOWED_SDE_CATEGORIES)
        excl_metas = ",".join(str(m) for m in EXCLUDED_META_GROUPS)
        excl_tids = ",".join(str(t) for t in EXCLUDED_TYPE_IDS)

        base_filter = f"""
            t.published = 1
            AND t.marketGroupID IS NOT NULL
            AND t.typeID NOT IN ({excl_tids})
            AND c.categoryName IN ({allowed_cats})
            AND COALESCE(mt.metaGroupID, 1) NOT IN ({excl_metas})
            AND {sql_excluded_market_groups_clause("t.marketGroupID")}
            AND {sql_non_expired_description_clause("t.description")}
        """

        rows = con.execute(f"""
            SELECT
                h.type_id,
                t.typeName,
                t.volume,
                t.groupID,
                g.groupName,
                AVG(h.volume) AS daily_units,
                AVG(h.average * h.volume) AS daily_isk
            FROM forge_market_history h
            JOIN sde.invTypes t ON h.type_id = t.typeID
            JOIN sde.invGroups g ON t.groupID = g.groupID
            JOIN sde.invCategories c ON g.categoryID = c.categoryID
            LEFT JOIN sde.invMetaTypes mt ON t.typeID = mt.typeID
            WHERE h.date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history)
              AND h.volume > 0
              AND {base_filter}
            GROUP BY h.type_id, t.typeName, t.volume, t.groupID, g.groupName
            HAVING AVG(h.volume) >= {float(min_units_day)}
               AND AVG(h.average * h.volume) >= {float(min_isk_day)}
            ORDER BY AVG(h.average * h.volume) DESC
            LIMIT {int(limit)}
        """).fetchall()

        seen = {int(r[0]) for r in rows}
        missing_extra = [int(t) for t in (extra_tids or []) if int(t) not in seen]
        if missing_extra:
            extra_str = ",".join(str(t) for t in missing_extra)
            extra_rows = con.execute(f"""
                SELECT
                    t.typeID,
                    t.typeName,
                    t.volume,
                    t.groupID,
                    g.groupName,
                    COALESCE(AVG(h.volume), 0) AS daily_units,
                    COALESCE(AVG(h.average * h.volume), 0) AS daily_isk
                FROM sde.invTypes t
                JOIN sde.invGroups g ON t.groupID = g.groupID
                JOIN sde.invCategories c ON g.categoryID = c.categoryID
                LEFT JOIN sde.invMetaTypes mt ON t.typeID = mt.typeID
                LEFT JOIN forge_market_history h
                  ON h.type_id = t.typeID
                 AND h.date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history)
                 AND h.volume > 0
                WHERE t.typeID IN ({extra_str})
                  AND {base_filter}
                GROUP BY t.typeID, t.typeName, t.volume, t.groupID, g.groupName
            """).fetchall()
            rows.extend(extra_rows)


    meta = {}
    for tid, name, raw_vol, gid, gname, d_units, d_isk in rows:
        name_s = str(name)
        grp_s = str(gname)
        if is_item_blacklisted(name_s, grp_s, bl_items, bl_groups, bl_meta):
            continue
        m3 = PACKAGED_SHIP_M3.get(int(gid), float(raw_vol or 0.01))
        meta[int(tid)] = {
            "name": name_s,
            "m3": max(m3, 0.01),
            "group_name": grp_s,
            "jita_vol_30d": float(d_units or 0.0),
            "jita_isk_30d": float(d_isk or 0.0),
        }
    return meta


def load_cached_import_suggestions() -> dict | None:
    return read_json(IMPORT_CACHE_FILE)


def _estimate_destination_daily_vol(
    tid: int,
    info: dict,
    s_entry: dict,
    scale_factor: float,
    obs_confidence: float,
    days_tracked: float,
    observed_by_type: dict,
) -> tuple[float, float | None, bool, str]:
    """Resolves expected daily destination unit volume from regional history or blended staging burn."""
    if s_entry["daily_vol"] is not None:
        return float(s_entry["daily_vol"]), None, False, "REGIONAL_30D"

    extrapolated_vol = info["jita_vol_30d"] * scale_factor
    sell_vol = s_entry["sell_vol"]
    obs_entry = observed_by_type.get(tid)

    if obs_confidence > 0.0 and obs_entry and obs_entry["real_daily_burn"] > 0:
        observed_daily = obs_entry["real_daily_burn"]
        blended = ((1.0 - obs_confidence) * extrapolated_vol) + (obs_confidence * observed_daily)
        return (
            blended,
            observed_daily,
            obs_confidence < 0.85,
            "OBSERVED" if obs_confidence >= 0.85 else f"BLENDED_{int(round(obs_confidence * 100))}PCT",
        )
    if obs_confidence > 0.0 and days_tracked >= 1.0 and sell_vol > 0:
        return (1.0 - obs_confidence) * extrapolated_vol, 0.0, True, f"DAMPENED_{int(round(obs_confidence * 100))}PCT"

    return extrapolated_vol, None, True, "EST_4.5PCT_JITA"


def _evaluate_import_candidate(
    tid: int,
    info: dict,
    jita_ask: float,
    s_entry: dict,
    dest: dict,
    rate_ov: float | None,
    my_struct_stock: dict,
    my_jita_hangar: dict[int, int],
    obs_confidence: float,
    days_tracked: float,
    observed_by_type: dict,
) -> dict | None:
    """Computes pricing, margin, freight, and stock-deducted restock quantities for one candidate."""
    is_structure = dest["type"] == "structure"
    est_daily_vol, observed_daily, vol_is_extrapolated, vol_source = _estimate_destination_daily_vol(
        tid, info, s_entry, dest.get("volume_scale_factor", 1.0), obs_confidence, days_tracked, observed_by_type
    )
    if est_daily_vol < dest.get("min_dest_units_day", 1.0):
        return None

    m3 = info["m3"]
    dest_ask = s_entry["lowest_ask"]
    sell_vol = s_entry["sell_vol"]
    top_buy = s_entry["top_buy"]

    freight_cost = calc_courier_freight(m3, jita_ask, dest, rate_override=rate_ov, apply_min=False)
    landed_cost = jita_ask + freight_cost

    if dest_ask and dest_ask > 0:
        list_price = round(dest_ask - 0.01, 2)
        status = ("LOW_STOCK" if sell_vol < est_daily_vol * 1.5 else "ACTIVE_BOOK") if is_structure else "UNDERCUT_LIST"
    elif is_structure and top_buy and top_buy > landed_cost * 1.15:
        list_price = round(max(top_buy * 1.15, jita_ask * 1.25), 2)
        status = "STOCKOUT_BUY_DEMAND"
    elif is_structure:
        list_price = round(jita_ask * 1.25, 2)
        status = "STOCKOUT_HIGH_VEL"
    else:
        return None

    exit_fee = dest["exit_fee"]
    net_rev = list_price * (1.0 - exit_fee)
    profit_unit = net_rev - landed_cost
    margin_pct = (profit_unit / landed_cost) * 100.0 if landed_cost > 0 else 0.0
    isk_per_m3 = profit_unit / m3

    if margin_pct < dest["min_margin_pct"] or isk_per_m3 < dest["min_isk_per_m3"] or profit_unit <= 0:
        return None
    if dest.get("max_markup_pct") is not None and margin_pct > dest["max_markup_pct"]:
        return None

    raw_qty = max(1, int(round(est_daily_vol * dest.get("target_days_supply", 3.0))))
    max_cap_qty = max(1, int(1.5e9 // max(jita_ask, 1.0)))
    target_qty = min(raw_qty, max_cap_qty)

    st_local = my_struct_stock.get(tid) or {"hangar_qty": 0, "sell_qty": 0}
    my_hangar_q = int(st_local["hangar_qty"])
    my_sell_q = int(st_local["sell_qty"])
    my_jita_q = int(my_jita_hangar.get(tid, 0))
    my_local_qty = my_hangar_q + my_sell_q + my_jita_q
    suggested_qty = max(0, target_qty - my_local_qty)

    if suggested_qty == 0:
        status = "FULLY_STOCKED"
    elif my_local_qty > 0:
        status = "RESTOCK"

    line_m3 = m3 * suggested_qty
    line_jita = jita_ask * suggested_qty
    solo_freight = calc_courier_freight(line_m3, line_jita, dest, rate_override=rate_ov, apply_min=True)
    solo_landed = line_jita + solo_freight
    solo_profit = (net_rev * suggested_qty) - solo_landed
    solo_margin_pct = (solo_profit / solo_landed) * 100.0 if solo_landed > 0 else 0.0

    row = {
        "destination": dest["name"],
        "location_id": dest["location_id"],
        "type_id": tid,
        "name": info["name"],
        "group": info["group_name"],
        "m3": round(m3, 2),
        "jita_ask": round(jita_ask, 2),
        "jita_vol_30d": round(info["jita_vol_30d"], 1),
        "jita_isk_30d": round(info["jita_isk_30d"], 0),
        "dest_ask": round(dest_ask, 2) if dest_ask else None,
        "dest_sell_vol": sell_vol,
        "target_qty": target_qty,
        "my_local_stock": my_local_qty,
        "my_hangar_qty": my_hangar_q,
        "my_sell_qty": my_sell_q,
        "my_jita_qty": my_jita_q,
        "dest_top_buy": round(top_buy, 2) if top_buy else None,
        "dest_daily_vol": round(est_daily_vol, 1),
        "observed_daily_vol": round(observed_daily, 2) if observed_daily is not None else None,
        "vol_extrapolated": vol_is_extrapolated,
        "vol_source": vol_source,
        "suggested_sell": list_price,
        "freight_cost": round(freight_cost, 2),
        "landed_cost": round(landed_cost, 2),
        "profit_unit": round(profit_unit, 2),
        "margin_pct": round(margin_pct, 1),
        "isk_per_m3": round(isk_per_m3, 1),
        "suggested_qty": suggested_qty,
        "est_daily_profit": round(profit_unit * est_daily_vol, 2),
        "est_total_profit": round(profit_unit * suggested_qty, 2),
        "solo_freight_cost": round(solo_freight, 2),
        "solo_margin_pct": round(solo_margin_pct, 1),
        "status": status,
    }
    if is_structure:
        row["structure_id"] = dest["location_id"]
    return row


def _build_destination_section(
    dest: dict,
    opportunities: list[dict],
    limit: int,
    rate_ov: float | None,
    meta_ctx: dict,
) -> dict:
    """Sorts opportunities, computes top-100 MultiBuy batch metrics, and builds the API section payload."""
    is_hisec_tiered = dest.get("pricing_model") == "hisec_tiered"
    is_structure = dest["type"] == "structure"
    exit_fee = dest["exit_fee"]

    opportunities.sort(
        key=lambda x: (x["suggested_qty"] > 0, x["est_daily_profit"], x["est_total_profit"]),
        reverse=True,
    )
    trimmed = opportunities[:limit]
    actionable = [x for x in trimmed if x["suggested_qty"] > 0]
    batch = actionable[:100]

    batch_m3 = sum(x["m3"] * x["suggested_qty"] for x in batch)
    batch_collateral = sum(x["jita_ask"] * x["suggested_qty"] for x in batch)
    batch_freight = calc_courier_freight(batch_m3, batch_collateral, dest, rate_override=rate_ov, apply_min=True) if batch else 0.0
    batch_net_rev = sum(x["suggested_sell"] * (1.0 - exit_fee) * x["suggested_qty"] for x in batch)
    batch_profit = batch_net_rev - batch_collateral - batch_freight

    all_m3 = sum(x["m3"] * x["suggested_qty"] for x in actionable)
    all_collateral = sum(x["jita_ask"] * x["suggested_qty"] for x in actionable)
    all_freight = calc_courier_freight(all_m3, all_collateral, dest, rate_override=rate_ov, apply_min=True) if actionable else 0.0

    section = {
        "location_id": dest["location_id"],
        "pricing_model": dest.get("pricing_model", "per_m3"),
        "warps": int(rate_ov) if (is_hisec_tiered and rate_ov is not None) else dest.get("warps"),
        "freight_rate_m3": None if is_hisec_tiered else (rate_ov if rate_ov is not None else dest["freight_rate_m3"]),
        "collateral_pct": dest.get("collateral_pct", 0.0),
        "min_freight": dest.get("min_freight", 0.0),
        "max_freight": dest.get("max_freight"),
        "volume_scale_factor": dest.get("volume_scale_factor", 1.0),
        "min_jita_isk_day": dest["min_jita_isk_day"],
        "count": len(trimmed),
        "multibuy_text": "\n".join(f"{x['name']} {x['suggested_qty']}" for x in batch),
        "multibuy_m3": round(batch_m3, 1),
        "multibuy_collateral": round(batch_collateral, 2),
        "multibuy_freight": round(batch_freight, 2),
        "multibuy_profit": round(batch_profit, 2),
        "all_m3": round(all_m3, 1),
        "all_collateral": round(all_collateral, 2),
        "all_freight": round(all_freight, 2),
        "items": trimmed,
    }
    if is_hisec_tiered:
        w = section["warps"]
        lw = int(dest.get("lowsec_warps", 0))
        section["split_plan_top20"] = calc_hisec_optimal_split(batch_m3, batch_collateral, w, lw)["plan"] if batch else "None"
        section["split_plan_all"] = calc_hisec_optimal_split(all_m3, all_collateral, w, lw)["plan"] if trimmed else "None"

    if is_structure:
        section.update(meta_ctx)
    else:
        section["station_id"] = dest["location_id"]

    return section


def check_cj6mt_seller_stock(structure_id: int = 1049588174021) -> dict:
    """
    Audits the C-J6MT seller character's current hangar inventory and active sell orders
    in 1st Taj Mahgoon against live structure asks and Jita 4-4 replacement prices.
    """
    char_name, token, char_id = resolve_role_character("cj6mt")
    structure_warning = None
    try:
        dest_book, _ = fetch_structure_order_book(structure_id, token)
    except Exception as e:
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", 0)
        dest_book = {}
        structure_warning = (
            f"Structure market returned HTTP {code} for {char_name}. "
            f"Click 'C-J6MT Seller (Switch)' to re-authorize with esi-markets.structure_markets.v1, "
            f"and confirm {char_name} has docking/market access to 1st Taj Mahgoon."
        )
    my_stock = fetch_character_locations_stock(char_id, token, [structure_id]).get(structure_id, {})

    tids = sorted(my_stock.keys())
    if not tids:
        return {
            "character_name": char_name,
            "character_id": char_id,
            "structure_id": structure_id,
            "total_lines": 0,
            "total_hangar_units": 0,
            "total_listed_units": 0,
            "est_shelf_value": 0.0,
            "est_hangar_value": 0.0,
            "structure_warning": structure_warning,
            "items": [],
        }

    sde_meta, _ = resolve_sde_items(type_ids=tids)

    session = create_session()
    jita_asks: dict[int, float | None] = {}
    ensure_jita_asks(session, tids, jita_asks)

    audit_rows = []
    tot_hangar_val = 0.0
    tot_shelf_val = 0.0

    for tid in tids:
        st = my_stock[tid]
        info = sde_meta.get(tid, {"name": f"Type {tid}", "unit_m3": 1.0, "group_name": "Unknown"})
        book_entry = dest_book.get(tid) or {}
        lowest_ask = book_entry.get("lowest_ask")
        jita_ask = jita_asks.get(tid) or 0.0

        rec_sell = (
            round(lowest_ask - 0.01, 2)
            if (lowest_ask and lowest_ask > 0)
            else (round(jita_ask * 1.25, 2) if jita_ask > 0 else 0.0)
        )
        hangar_qty = st["hangar_qty"]
        sell_qty = st["sell_qty"]
        my_sell = st["my_lowest_sell"]

        hangar_val = hangar_qty * (rec_sell or jita_ask)
        shelf_val = sell_qty * (my_sell or rec_sell or jita_ask)
        tot_hangar_val += hangar_val
        tot_shelf_val += shelf_val

        if hangar_qty > 0 and sell_qty == 0:
            status = "UNLISTED_HANGAR"
        elif my_sell and lowest_ask and my_sell > lowest_ask + 0.01:
            status = "UNDERCUT"
        elif hangar_qty > 0 and sell_qty > 0:
            status = "ACTIVE + RESERVE"
        else:
            status = "TOP_ASK"

        audit_rows.append({
            "type_id": tid,
            "name": info["name"],
            "group": info.get("group_name", "Unknown"),
            "hangar_qty": hangar_qty,
            "sell_qty": sell_qty,
            "total_qty": hangar_qty + sell_qty,
            "my_sell_price": round(my_sell, 2) if my_sell else None,
            "structure_lowest_ask": round(lowest_ask, 2) if lowest_ask else None,
            "recommended_sell": rec_sell,
            "jita_ask": round(jita_ask, 2) if jita_ask else None,
            "est_total_value": round(hangar_val + shelf_val, 2),
            "status": status,
        })

    audit_rows.sort(key=lambda x: x["est_total_value"], reverse=True)
    return {
        "character_name": char_name,
        "character_id": char_id,
        "structure_id": structure_id,
        "total_lines": len(audit_rows),
        "total_hangar_units": sum(x["hangar_qty"] for x in audit_rows),
        "total_listed_units": sum(x["sell_qty"] for x in audit_rows),
        "est_shelf_value": round(tot_shelf_val, 2),
        "est_hangar_value": round(tot_hangar_val, 2),
        "structure_warning": structure_warning,
        "items": audit_rows,
    }


def scan_import_opportunities(
    amarr_warps: int | None = None,
    cj6mt_rate: float | None = None,
    limit: int = 100,
) -> dict:
    rate_overrides = {"amarr": amarr_warps, "cj6mt": cj6mt_rate}
    session = create_session()
    jita_asks: dict[int, float | None] = {}
    results = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}

    primary_name, primary_tok, primary_id = resolve_primary_character()
    jita_desk_tids = get_desk_claimed_type_ids()
    primary_locs = fetch_character_locations_stock(primary_id, primary_tok, [JITA_STATION_ID, 60008494])

    for dest in IMPORT_DESTINATIONS:
        key = dest["key"]
        rate_ov = rate_overrides.get(key)
        is_structure = dest["type"] == "structure"
        min_dest_units = dest.get("min_dest_units_day", 1.0)

        my_jita_hangar: dict[int, int] = {
            tid_k: int(st_v.get("hangar_qty", 0))
            for tid_k, st_v in primary_locs.get(JITA_STATION_ID, {}).items()
            if tid_k not in jita_desk_tids
        }

        if is_structure:
            char_name, access_token, char_id = resolve_role_character("cj6mt", dest["character_name"])
            dest_book, raw_orders = fetch_structure_order_book(dest["location_id"], access_token)
            seller_locs = fetch_character_locations_stock(char_id, access_token, [dest["location_id"], JITA_STATION_ID])
            my_struct_stock = seller_locs.get(dest["location_id"], {})
            for tid_k, st_v in seller_locs.get(JITA_STATION_ID, {}).items():
                my_jita_hangar[tid_k] = my_jita_hangar.get(tid_k, 0) + int(st_v.get("hangar_qty", 0))

            diff_summary = process_snapshot_diff(dest["location_id"], raw_orders)
            observed_stats = get_observed_staging_velocity(dest["location_id"])
            proven_tids = [
                tid for tid, st in observed_stats["by_type"].items()
                if observed_stats["days_tracked"] >= 2.0 and st["real_daily_burn"] >= min_dest_units
            ]
            meta = _get_jita_high_velocity_candidates(
                min_isk_day=dest["min_jita_isk_day"],
                min_units_day=dest["min_jita_units_day"],
                limit=dest["candidate_limit"],
                extra_tids=proven_tids,
            )
            ensure_jita_asks(session, list(meta.keys()), jita_asks)
        else:
            observed_stats = {"days_tracked": 0.0, "snapshot_count": 0, "by_type": {}}
            meta = _get_jita_high_velocity_candidates(
                min_isk_day=dest["min_jita_isk_day"],
                min_units_day=dest["min_jita_units_day"],
                limit=dest["candidate_limit"],
            )
            ensure_jita_asks(session, list(meta.keys()), jita_asks)
            dest_book = _fetch_station_order_book(session, dest, list(meta.keys()), jita_asks)
            char_name, char_id = primary_name, primary_id
            my_struct_stock = primary_locs.get(dest["location_id"], {})
            diff_summary = None

        days_tracked = observed_stats["days_tracked"]
        obs_confidence = min(1.0, days_tracked / 7.0) if days_tracked >= 0.25 else 0.0

        opportunities = []
        for tid, info in meta.items():
            jita_ask = jita_asks.get(tid)
            if not jita_ask or jita_ask <= 0:
                continue
            s_entry = dest_book.get(tid)
            if not s_entry and not is_structure:
                continue
            s_entry = s_entry or {"lowest_ask": None, "sell_vol": 0, "top_buy": None, "daily_vol": None}

            row = _evaluate_import_candidate(
                tid=tid,
                info=info,
                jita_ask=jita_ask,
                s_entry=s_entry,
                dest=dest,
                rate_ov=rate_ov,
                my_struct_stock=my_struct_stock,
                my_jita_hangar=my_jita_hangar,
                obs_confidence=obs_confidence,
                days_tracked=days_tracked,
                observed_by_type=observed_stats["by_type"],
            )
            if row is not None:
                opportunities.append(row)

        meta_ctx = {
            "structure_id": dest["location_id"],
            "character_id": char_id,
            "character_name": char_name,
            "seller_stock_types": len(my_struct_stock),
            "total_structure_types": len(dest_book),
            "tracking_days": days_tracked,
            "tracking_snapshots": observed_stats["snapshot_count"],
            "tracking_confidence_pct": round(obs_confidence * 100.0, 1),
            "last_diff": diff_summary,
        }
        results[key] = _build_destination_section(dest, opportunities, limit, rate_ov, meta_ctx)

    write_json(IMPORT_CACHE_FILE, results)

    return results
