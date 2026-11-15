from services.ledgers import get_annotated_desks, get_desk_profile
from services.accounting import round_down_eve, calc_max_profitable_buy, round_up_eve, eve_tick, calc_projected_margin

def audit_missing_buys(desk: str = "all") -> dict:
    """
    Audits UNPLACED orders for `desk` against live Jita 4-4 ESI order books using
    each item's DeskProfile margin floor and centralized EVE tick rounding.
    """
    from concurrent.futures import ThreadPoolExecutor
    from services.esi import fetch_jita_book_top

    eg, tac, hld = get_annotated_desks()
    desk_map = {
        "evergreen": eg,
        "tactical": tac,
        "hold": hld,
        "all": eg + tac + hld,
    }
    rows = desk_map.get(desk.lower(), eg + tac + hld)
    unplaced = [r for r in rows if r.get("char_status") == "UNPLACED"]
    if not unplaced:
        return {"total_items": 0, "total_escrow": 0.0, "items": [], "multibuy_text": ""}

    tids = list({int(r["type_id"]) for r in unplaced if r.get("type_id")})
    books = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tid, top_b, low_s in ex.map(fetch_jita_book_top, tids):
            books[tid] = (top_b, low_s)

    out_items = []
    for r in unplaced:
        tid = int(r["type_id"])
        qty = int(r.get("qty") or 1)
        target_buy = float(r.get("target_buy") or 0.0)
        target_sell = float(r.get("target_sell") or 0.0)
        top_buy, live_ask = books.get(tid, (0.0, 0.0))

        eff_sell = live_ask if live_ask > 0 else target_sell
        if eff_sell <= 0:
            continue

        cfg = get_desk_profile(r.get("desk_type") or desk)
        max_buy_floor = round_down_eve(calc_max_profitable_buy(eff_sell, cfg.live_min_margin))
        if max_buy_floor <= 0:
            continue

        desired_buy = round_up_eve(top_buy + eve_tick(top_buy)) if top_buy > 0 else target_buy
        desired_buy = max(desired_buy, target_buy)

        if desired_buy <= max_buy_floor:
            rec_buy = desired_buy
            squeezed = False
        else:
            rec_buy = max(target_buy, max_buy_floor) if target_buy <= eff_sell * 0.90 else max_buy_floor
            squeezed = True

        margin_pct = round(calc_projected_margin(rec_buy, eff_sell), 1)
        line_escrow = round(rec_buy * qty, 2)

        out_items.append({
            "type_id": tid,
            "name": r.get("name"),
            "qty": qty,
            "top_buy": round(top_buy, 2),
            "recommended_buy": round(rec_buy, 2),
            "margin_pct": margin_pct,
            "line_escrow": line_escrow,
            "squeezed": squeezed,
        })

    out_items.sort(key=lambda x: (x["squeezed"], -x["line_escrow"]))
    multibuy_text = "\n".join(f"{it['name']} {it['qty']}" for it in out_items)
    total_escrow = round(sum(it["line_escrow"] for it in out_items), 2)

    return {
        "total_items": len(out_items),
        "total_escrow": total_escrow,
        "items": out_items,
        "multibuy_text": multibuy_text,
    }
