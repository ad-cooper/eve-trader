from concurrent.futures import ThreadPoolExecutor
from services.ledgers import get_annotated_desks, get_desk_profile
from services.esi import fetch_jita_book_top
from services.accounting import (
    BUY_SCC_SURCHARGE,
    TOTAL_EXIT_FEE,
    eve_tick,
    round_up_eve,
    round_down_eve,
    calc_max_profitable_buy,
    calc_line_financials,
)


def audit_missing_buys(desk: str = "all") -> dict:
    """
    Audits UNPLACED orders for `desk` against live Jita 4-4 ESI order books using
    each item's DeskProfile margin floor and centralized EVE tick rounding.
    """
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
        return {
            "total_items": 0,
            "total_escrow": 0.0,
            "total_isk_needed": 0.0,
            "total_gross_exit": 0.0,
            "total_expected_profit": 0.0,
            "blended_roi_pct": 0.0,
            "total_buy_fees": 0.0,
            "total_exit_fees": 0.0,
            "total_fees": 0.0,
            "items": [],
            "multibuy_text": "",
        }

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

        if top_buy > 0:
            desired_buy = round_up_eve(top_buy + eve_tick(top_buy))
        else:
            desired_buy = round_down_eve(target_buy) if target_buy > 0 else max_buy_floor

        if desired_buy <= max_buy_floor:
            rec_buy = desired_buy
            squeezed = False
        else:
            rec_buy = max_buy_floor
            squeezed = True

        rec_buy = round(rec_buy, 2)
        eff_sell = round(eff_sell, 2)
        fin = calc_line_financials(rec_buy, eff_sell, qty=qty)

        line_escrow = round(rec_buy * qty, 2)
        line_buy_fee = round(rec_buy * BUY_SCC_SURCHARGE * qty, 2)
        line_isk_needed = round(line_escrow + line_buy_fee, 2)
        line_gross_exit = round(eff_sell * qty, 2)
        line_exit_fee = round(eff_sell * TOTAL_EXIT_FEE * qty, 2)
        line_total_fees = round(line_buy_fee + line_exit_fee, 2)
        line_profit = fin["profit"]

        out_items.append({
            "type_id": tid,
            "name": r.get("name"),
            "desk": str(r.get("desk_type") or desk).upper(),
            "qty": qty,
            "top_buy": round(top_buy, 2),
            "recommended_buy": rec_buy,
            "expected_sell": eff_sell,
            "margin_pct": fin["net_margin"],
            "line_escrow": line_escrow,
            "line_isk_needed": line_isk_needed,
            "line_buy_fee": line_buy_fee,
            "line_exit_fee": line_exit_fee,
            "line_total_fees": line_total_fees,
            "line_gross_exit": line_gross_exit,
            "line_profit": line_profit,
            "squeezed": squeezed,
        })

    out_items.sort(key=lambda x: (x["squeezed"], -x["line_escrow"]))
    multibuy_text = "\n".join(f"{it['name']} {it['qty']}" for it in out_items)

    total_escrow = round(sum(it["line_escrow"] for it in out_items), 2)
    total_buy_fees = round(sum(it["line_buy_fee"] for it in out_items), 2)
    total_isk_needed = round(sum(it["line_isk_needed"] for it in out_items), 2)
    total_exit_fees = round(sum(it["line_exit_fee"] for it in out_items), 2)
    total_fees = round(total_buy_fees + total_exit_fees, 2)
    total_gross_exit = round(sum(it["line_gross_exit"] for it in out_items), 2)
    total_expected_profit = round(sum(it["line_profit"] for it in out_items), 2)
    blended_roi_pct = round((total_expected_profit / total_isk_needed) * 100.0, 1) if total_isk_needed > 0 else 0.0

    return {
        "total_items": len(out_items),
        "total_escrow": total_escrow,
        "total_isk_needed": total_isk_needed,
        "total_gross_exit": total_gross_exit,
        "total_expected_profit": total_expected_profit,
        "blended_roi_pct": blended_roi_pct,
        "total_buy_fees": total_buy_fees,
        "total_exit_fees": total_exit_fees,
        "total_fees": total_fees,
        "items": out_items,
        "multibuy_text": multibuy_text,
    }
