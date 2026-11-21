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

    # Mild dip where live ask (99.5M) still leaves +2.72% margin (>= 2.0% cancel_floor) stays active
    code_mild, _ = evaluate_order_action(
        desk_type="evergreen",
        my_price=92_000_000.0,
        top_buy=92_000_000.0,
        t_sell=99_500_000.0,
        dump_freq=65.0,
        csv_sell=105_000_000.0,
        live_ask=99_500_000.0,
    )
    assert code_mild in ("TOP_BOOK", "PATIENT")

    # Compressed live ask (98M -> +1.17% < 2.0% cancel_floor) cancels despite stale 105M csv_sell
    code_comp, label_comp = evaluate_order_action(
        desk_type="evergreen",
        my_price=92_000_000.0,
        top_buy=92_000_000.0,
        t_sell=98_000_000.0,
        dump_freq=65.0,
        csv_sell=105_000_000.0,
        live_ask=98_000_000.0,
    )
    assert code_comp == "CANCEL_OVERPRICED"
    assert "CANCEL" in label_comp



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


def test_jita_buy_order_reachability_filtering():
    from services.esi import extract_top_buy_for_station

    orders = [
        # Station-only order in Urlen (different system) -> must NOT match Jita 4-4
        {"location_id": 60003763, "system_id": 30000140, "range": "station", "price": 500_000_000.0, "is_buy_order": True},
        # Direct Jita 4-4 order -> matches
        {"location_id": 60003760, "system_id": 30000142, "range": "station", "price": 100_000_000.0, "is_buy_order": True},
        # Jita 4-2 solar-system order -> matches Jita 4-4
        {"location_id": 60003758, "system_id": 30000142, "range": "solarsystem", "price": 105_000_000.0, "is_buy_order": True},
        # Perimeter 1-jump order -> matches Jita 4-4
        {"location_id": 60004456, "system_id": 30000144, "range": "1", "price": 108_000_000.0, "is_buy_order": True},
    ]
    assert extract_top_buy_for_station(orders) == 108_000_000.0

    # When only unreachable remote station orders exist, return None instead of poisoning the bid
    remote_only = [
        {"location_id": 60003763, "system_id": 30000140, "range": "station", "price": 500_000_000.0, "is_buy_order": True},
        {"location_id": 60004456, "system_id": 30000144, "range": "station", "price": 450_000_000.0, "is_buy_order": True},
    ]
    assert extract_top_buy_for_station(remote_only) is None


def test_wallet_transactions_cache_avoids_duplicate_esi_calls(monkeypatch, tmp_path):
    import services.esi as esi_mod

    cache_file = tmp_path / "wallet_tx.json"
    monkeypatch.setattr(esi_mod, "WALLET_TX_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(esi_mod, "_MEM_WALLET_TX_CACHE", {"character_id": None, "timestamp": 0.0, "transactions": []})
    monkeypatch.setattr(esi_mod, "load_sso_tokens", lambda *a, **kw: {"character_id": 123456})
    monkeypatch.setattr(esi_mod, "get_valid_access_token", lambda *a, **kw: "valid_token")

    http_calls = []

    class DummyResp:
        status_code = 200
        def json(self):
            return [{"transaction_id": 1, "type_id": 34, "quantity": 100, "unit_price": 5.0, "is_buy": True}]

    def fake_get(url, headers=None, timeout=15):
        http_calls.append(url)
        return DummyResp()

    monkeypatch.setattr(esi_mod.requests, "get", fake_get)

    first = esi_mod.fetch_wallet_transactions()
    second = esi_mod.fetch_wallet_transactions()
    assert len(first) == 1 and second == first
    assert len(http_calls) == 1


def test_update_ledger_market_prices_uses_bulk_asks_and_targeted_esi(monkeypatch, tmp_path):
    import services.market as mkt_mod

    cache_out = tmp_path / "market_cache.json"
    monkeypatch.setattr(mkt_mod, "MARKET_CACHE_FILE", str(cache_out))
    monkeypatch.setattr(
        mkt_mod,
        "load_all_desk_ledgers",
        lambda: {"evergreen": [{"type_id": 10}, {"type_id": 20}, {"type_id": 30}], "tactical": [], "hold": []},
    )
    monkeypatch.setattr(
        mkt_mod,
        "load_cached_orders",
        lambda: {"buys": {"10": {"price": 90.0}}, "sells": {"20": {"price": 110.0}}},
    )
    monkeypatch.setattr(mkt_mod, "fetch_station_asks_bulk", lambda session, station_id, tids: {10: 100.0, 20: 112.0, 30: 50.0})

    esi_polled = []
    def fake_jita_book(tid, session=None):
        esi_polled.append(tid)
        return tid, 91.0, 99.5

    monkeypatch.setattr(mkt_mod, "fetch_jita_book_top", fake_jita_book)

    result = mkt_mod.update_ledger_market_prices()
    # Only type_id 10 (the active buy order) should hit ESI; 20 & 30 are satisfied by bulk Fuzzwork asks
    assert esi_polled == [10]
    assert result["10"]["top_buy"] == 91.0
    assert result["10"]["lowest_ask"] == 99.5
    assert result["20"]["lowest_ask"] == 112.0
    assert result["30"]["lowest_ask"] == 50.0


def test_daily_pipeline_orchestrates_all_steps(monkeypatch):
    import workflows.daily_pipeline as dp

    calls = []

    class DummyCompleted:
        returncode = 0

    def fake_subproc(cmd, check=False):
        calls.append(("subprocess", tuple(cmd), check))
        return DummyCompleted()

    monkeypatch.setattr(dp.subprocess, "run", fake_subproc)
    monkeypatch.setattr(dp, "fetch_character_orders", lambda: calls.append(("fetch_character_orders",)))
    monkeypatch.setattr(dp, "update_ledger_market_prices", lambda: calls.append(("update_ledger_market_prices",)))
    monkeypatch.setattr(dp, "calculate_performance", lambda: calls.append(("calculate_performance",)))
    monkeypatch.setattr(dp, "scan_import_opportunities", lambda: calls.append(("scan_import_opportunities",)))

    ok, ts = dp.run_daily_ledger_rebuild(include_imports=True)
    assert ok is True
    assert ts and ts.endswith("UTC")
    assert calls == [
        ("subprocess", ("python3", "scripts/sync_market_data.py"), False),
        ("subprocess", ("python3", "scripts/generate_ledgers.py", "--desk", "all", "--capital", "auto"), True),
        ("fetch_character_orders",),
        ("update_ledger_market_prices",),
        ("calculate_performance",),
        ("scan_import_opportunities",),
    ]


def test_order_actions_bumps_and_cancels(monkeypatch):
    import workflows.order_actions as oa

    eg_rows = [
        {
            "type_id": 1001,
            "name": "Viable Buy Bump Item",
            "desk_type": "evergreen",
            "char_status": "BUY_ACTIVE",
            "action_code": "BUMP_VIABLE",
            "action_text": "BUMP VIABLE (+8.2%)",
            "my_price": 90_000_000.0,
            "my_qty": 2,
            "escrow": 180_000_000.0,
            "target_buy": 91_000_000.0,
            "target_sell": 105_000_000.0,
        },
        {
            "type_id": 1002,
            "name": "Overpriced Buy Cancel Item",
            "desk_type": "evergreen",
            "char_status": "BUY_ACTIVE",
            "action_code": "CANCEL_OVERPRICED",
            "action_text": "OVERBID (0.4% MARGIN - CANCEL)",
            "my_price": 99_000_000.0,
            "my_qty": 3,
            "escrow": 297_000_000.0,
            "target_buy": 95_000_000.0,
            "target_sell": 105_000_000.0,
        },
        {
            "type_id": 1003,
            "name": "Undercut Sell Item",
            "desk_type": "evergreen",
            "char_status": "SELL_ACTIVE",
            "action_code": "SELL_ACTIVE",
            "my_price": 115_000_000.0,
            "my_qty": 4,
            "target_buy": 95_000_000.0,
            "target_sell": 115_000_000.0,
        },
    ]

    monkeypatch.setattr(oa, "get_annotated_desks", lambda: (eg_rows, [], []))
    monkeypatch.setattr(
        oa,
        "load_cached_orders",
        lambda: {
            "buys": {"1001": {"price": 90_000_000.0, "volume_remain": 2}, "1002": {"price": 99_000_000.0, "volume_remain": 3}},
            "sells": {"1003": {"price": 115_000_000.0, "volume_remain": 4}},
        },
    )
    monkeypatch.setattr(
        oa,
        "load_market_prices_cache",
        lambda: {
            "1001": {"top_buy": 91_000_000.0, "lowest_ask": 105_000_000.0},
            "1002": {"top_buy": 99_500_000.0, "lowest_ask": 105_000_000.0},
            "1003": {"top_buy": 95_000_000.0, "lowest_ask": 112_000_000.0},
        },
    )
    monkeypatch.setattr(oa, "load_active_blacklist", lambda: (set(), set(), {}))
    monkeypatch.setattr(oa, "get_cost_basis_map", lambda tids, _: {1003: {"unit_cost": 95_000_000.0, "source": "WALLET"}})

    res = oa.get_order_actions(desk="all", refresh_live=False)
    assert res["bump_count"] == 2
    assert res["cancel_count"] == 1
    assert res["total_escrow_freed"] == 297_000_000.0
    assert res["total_bump_escrow_delta"] > 0
    sides = {b["type_id"]: b["side"] for b in res["bumps"]}
    assert sides == {1001: "BUY", 1003: "SELL"}
    assert res["cancels"][0]["type_id"] == 1002


def test_profitable_hold_and_weekend_dip_orders_are_not_cancelled():
    # Hold buy order at +23.9% margin stays PATIENT even when live ask recovers >92% of 180d target
    code_hold, label_hold = evaluate_order_action(
        desk_type="hold",
        my_price=71_070_000.0,
        top_buy=72_850_000.0,
        t_sell=92_770_000.0,
        dump_freq=20.0,
        proximity_pct=96.0,
        csv_sell=92_770_000.0,
        live_ask=89_000_000.0,
    )
    assert code_hold == "PATIENT"
    assert "HOLD FIRM" in label_hold

    # Evergreen buy order at 50.13M when live ask crashes to 48M (-9.06% live margin) MUST cancel
    code_eg, label_eg = evaluate_order_action(
        desk_type="evergreen",
        my_price=50_130_000.0,
        top_buy=50_400_000.0,
        t_sell=52_970_000.0,
        dump_freq=65.0,
        proximity_pct=90.6,
        csv_sell=56_500_000.0,
        live_ask=48_000_000.0,
    )
    assert code_eg == "CANCEL_OVERPRICED"
    assert "CANCEL" in label_eg



def test_missing_buys_never_overpays_stale_csv_target_buy(monkeypatch):
    import workflows.buy_orders as bo

    unplaced_rows = [
        {
            "type_id": 2001,
            "name": "Pith X-Type EM Shield Hardener",
            "desk_type": "evergreen",
            "char_status": "UNPLACED",
            "qty": 10,
            "target_buy": 87_000_000.0,   # Stale higher CSV target
            "target_sell": 97_000_000.0,
        },
        {
            "type_id": 2002,
            "name": "Margin Capped Module",
            "desk_type": "evergreen",
            "char_status": "UNPLACED",
            "qty": 5,
            "target_buy": 95_000_000.0,
            "target_sell": 100_000_000.0,
        },
    ]

    monkeypatch.setattr(bo, "get_annotated_desks", lambda: (unplaced_rows, [], []))
    monkeypatch.setattr(
        bo,
        "fetch_jita_book_top",
        lambda tid: (tid, 84_900_000.0, 97_000_000.0) if tid == 2001 else (tid, 96_000_000.0, 100_000_000.0),
    )

    res = bo.audit_missing_buys(desk="all")
    by_id = {it["type_id"]: it for it in res["items"]}

    # Must bid exactly 1 EVE tick above 84.90M (84.91M), NOT the stale 87.0M CSV target
    assert by_id[2001]["recommended_buy"] == 84_910_000.0
    assert by_id[2001]["squeezed"] is False

    # When top_buy exceeds margin floor, must cap at max_buy_floor and flag squeezed=True
    assert by_id[2002]["recommended_buy"] < 96_000_000.0
    assert by_id[2002]["squeezed"] is True
