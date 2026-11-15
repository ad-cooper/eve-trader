from datetime import datetime, timezone, timedelta
import pandas as pd
from services.accounting import (
    eve_tick,
    round_up_eve,
    round_down_eve,
    calc_line_financials,
    match_fifo_lots,
)
from services.ledgers import get_desk_profile, evaluate_order_action
from services.staging_diff import _detect_order_fill_events


def test_eve_tick_and_rounding():
    assert eve_tick(141_000_000.0) == 100_000.0
    assert round_up_eve(141_050_000.0) == 141_100_000.0
    assert round_down_eve(141_050_000.0) == 141_000_000.0
    assert round_up_eve(12.341) == 12.35
    assert round_down_eve(12.349) == 12.34


def test_line_financials_and_fifo():
    fin = calc_line_financials(buy_price=100_000_000.0, sell_price=120_000_000.0, qty=2)
    assert fin["profit"] > 0
    assert fin["net_margin"] > 10.0

    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=4)
    lots = match_fifo_lots([
        {"key": 1001, "date": t0, "is_buy": True, "qty": 3, "price": 50_000_000.0},
        {"key": 1001, "date": t1, "is_buy": False, "qty": 2, "price": 65_000_000.0},
    ])
    assert len(lots) == 1
    assert lots[0]["qty"] == 2
    assert lots[0]["hold_days"] == 4.0
    assert lots[0]["net_profit"] > 0


def test_weekend_dip_hysteresis_prevents_false_cancel():
    eg = get_desk_profile("evergreen")
    assert eg.cancel_floor < eg.live_min_margin

    code, label = evaluate_order_action(
        desk_type="evergreen",
        my_price=92_000_000.0,
        top_buy=92_000_000.0,
        t_sell=98_000_000.0,
        dump_freq=65.0,
        csv_sell=105_000_000.0,
        live_ask=98_000_000.0,
    )
    assert code == "PATIENT"
    assert "WEEKEND DIP" in label

    code_outbid, label_outbid = evaluate_order_action(
        desk_type="evergreen",
        my_price=92_000_000.0,
        top_buy=102_000_000.0,
        t_sell=105_000_000.0,
        dump_freq=65.0,
        csv_sell=105_000_000.0,
        live_ask=105_000_000.0,
    )
    assert code_outbid == "PATIENT"
    assert "OUTBID" in label_outbid


def test_staging_diff_detects_all_fill_archetypes():
    t_prev = datetime(2026, 11, 15, 18, 0, 0)
    t_now = datetime(2026, 11, 15, 18, 30, 0)
    df_old = pd.DataFrame([
        {"order_id": 1, "type_id": 100, "price": 50e6, "volume_remain": 10, "volume_total": 10, "issued": t_prev - timedelta(days=1), "duration": 90},
        {"order_id": 2, "type_id": 200, "price": 80e6, "volume_remain": 2, "volume_total": 2, "issued": t_prev - timedelta(days=1), "duration": 90},
    ])
    df_new = pd.DataFrame([
        {"order_id": 1, "type_id": 100, "price": 50e6, "volume_remain": 7, "volume_total": 10, "issued": str(t_prev - timedelta(days=1)), "duration": 90},
        {"order_id": 3, "type_id": 300, "price": 20e6, "volume_remain": 4, "volume_total": 5, "issued": str(t_prev), "duration": 90},
    ])
    events = _detect_order_fill_events(1049588174021, df_old, df_new, t_now, t_prev)
    kinds = [e[-1] for e in events]
    assert kinds == ["PARTIAL_FILL", "FULL_BUYOUT", "INTRA_SCAN_FILL"]
