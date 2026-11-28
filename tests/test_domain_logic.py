from datetime import datetime, timezone, timedelta
import pandas as pd
from services.accounting import (
    eve_tick,
    round_up_eve,
    round_down_eve,
    calc_line_financials,
    match_fifo_lots,
)
from services.desk import Desk
from services.esi import (
    extract_top_buy_for_station
)
from services.staging_diff import _detect_order_fill_events
from services.strategy_versions import (
    compute_book_depth_stats,
    v2_evaluate_order_action
)

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
    eg = Desk.get_desk_profile("evergreen")
    assert eg.cancel_floor < eg.live_min_margin

    # Mild dip where live ask (99.5M) still leaves +2.72% margin (>= 2.0% cancel_floor) stays active
    code_mild, _ = v2_evaluate_order_action(
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
    code_comp, label_comp = v2_evaluate_order_action(
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
    import services.sso as sso_mod

    cache_file = tmp_path / "wallet_tx.json"
    monkeypatch.setattr(esi_mod, "WALLET_TX_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(esi_mod, "_MEM_WALLET_TX_CACHE", {"character_id": None, "timestamp": 0.0, "transactions": []})

    fake_tokens = lambda *a, **kw: {"character_id": 123456}
    fake_token = lambda *a, **kw: "valid_token"
    monkeypatch.setattr(sso_mod, "load_sso_tokens", fake_tokens)
    monkeypatch.setattr(sso_mod, "get_valid_access_token", fake_token)
    monkeypatch.setattr(esi_mod, "load_sso_tokens", fake_tokens)
    monkeypatch.setattr(esi_mod, "get_valid_access_token", fake_token)

    request_calls = []

    class DummyResp:
        status_code = 200
        text = ""

        def json(self):
            return [{"transaction_id": 1, "type_id": 34, "quantity": 100, "unit_price": 5.0, "is_buy": True}]

    class DummySession:
        def request(self, method, url, *args, **kwargs):
            request_calls.append((method, url, kwargs.get("params"), kwargs.get("headers")))
            return DummyResp()

    monkeypatch.setattr(esi_mod, "get_esi_session", lambda: DummySession())

    first = esi_mod.fetch_wallet_transactions()
    second = esi_mod.fetch_wallet_transactions()
    assert len(first) == 1 and second == first
    assert len(request_calls) == 1

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

    monkeypatch.setattr(mkt_mod, "fetch_station_book_top", fake_jita_book)

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
        ("subprocess", ("python3", "scripts/generate_ledgers.py", "--desk", "all", "--capital", "auto", "--version", "all"), True),
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
    code_hold, label_hold = v2_evaluate_order_action(
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
    code_eg, label_eg = v2_evaluate_order_action(
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

    from services.accounting import calc_max_profitable_buy, round_down_eve, round_up_eve

    eg_max = round_down_eve(calc_max_profitable_buy(100_000_000.0, Desk.get_desk_profile("evergreen").live_min_margin))
    near_top_buy = round_up_eve(eg_max * 1.015)

    monkeypatch.setattr(bo, "get_annotated_desks", lambda: (unplaced_rows, [], []))
    monkeypatch.setattr(
        bo,
        "fetch_station_book_top",
        lambda tid: (tid, 84_900_000.0, 97_000_000.0) if tid == 2001 else (tid, near_top_buy, 100_000_000.0),
    )

    res = bo.audit_missing_buys(desk="all")
    by_id = {it["type_id"]: it for it in res["items"]}

    # Must bid exactly 1 EVE tick above 84.90M (84.91M), NOT the stale 87.0M CSV target
    assert by_id[2001]["recommended_buy"] == 84_910_000.0
    assert by_id[2001]["squeezed"] is False

    # When top_buy slightly exceeds margin floor (<=3% gap), must cap at max_buy_floor and flag squeezed=True
    assert by_id[2002]["recommended_buy"] == eg_max
    assert by_id[2002]["squeezed"] is True


def test_hold_desk_dip_orders_evaluate_against_recovery_target():
    # Hold dip order with +26.5% upside to 180d target (83M) stays active even when depressed dip live_ask is 67M
    code, _ = v2_evaluate_order_action(
        desk_type="hold",
        my_price=62_540_000.0,
        top_buy=63_830_000.0,
        t_sell=83_300_000.0,
        dump_freq=20.0,
        proximity_pct=80.4,
        csv_sell=83_300_000.0,
        live_ask=67_000_000.0,
    )
    assert code in ("BUMP_VIABLE", "PATIENT", "TOP_BOOK")


def test_hangar_timestop_or_loss_cancels_active_buy_order(monkeypatch):
    import workflows.order_actions as oa

    eg_rows = [
        {
            "type_id": 3001,
            "name": "'Peace' Large Remote Armor Repairer",
            "desk_type": "evergreen",
            "char_status": "BUY_ACTIVE",
            "action_code": "TOP_BOOK",
            "action_text": "TOP BID (WINNING)",
            "my_price": 177_900_000.0,
            "my_qty": 1,
            "escrow": 177_900_000.0,
            "target_buy": 177_900_000.0,
            "target_sell": 205_000_000.0,
        },
    ]

    monkeypatch.setattr(oa, "get_annotated_desks", lambda: (eg_rows, [], []))
    monkeypatch.setattr(oa, "load_cached_orders", lambda: {"buys": {"3001": {"price": 177_900_000.0, "volume_remain": 1}}, "sells": {}})
    monkeypatch.setattr(oa, "load_market_prices_cache", lambda: {"3001": {"top_buy": 177_900_000.0, "lowest_ask": 189_900_000.0}})
    monkeypatch.setattr(oa, "load_active_blacklist", lambda: (set(), set(), {}))
    monkeypatch.setattr(oa, "load_cached_assets", lambda max_age_seconds=600: [{"type_id": 3001, "quantity": 1}])
    monkeypatch.setattr(oa, "resolve_type_ids", lambda tids: {3001: "'Peace' Large Remote Armor Repairer"})
    monkeypatch.setattr(oa, "get_cost_basis_map", lambda tids, _: {3001: {"unit_cost": 221_600_000.0, "source": "WALLET"}})
    monkeypatch.setattr(oa, "get_hold_desk_params", lambda tids, nm, hm: {3001: {"days_held": 17, "max_days": 3, "is_expired": True}})

    res = oa.get_order_actions(desk="all", refresh_live=False)
    assert res["buy_bump_count"] == 0
    assert res["cancel_count"] == 1
    assert res["cancels"][0]["type_id"] == 3001
    assert "TIME STOP" in res["cancels"][0]["reason"]


def test_partial_stub_and_tactical_target_met_cancel_remaining_buy(monkeypatch):
    import workflows.order_actions as oa

    tac_rows = [
        {
            "type_id": 4001,
            "name": "Imperial Navy 200mm Steel Plates",
            "desk_type": "tactical",
            "char_status": "BUY_ACTIVE",
            "action_code": "PATIENT",
            "action_text": "OUTBID (HOLD +12.4%)",
            "my_price": 10_150_000.0,
            "my_qty": 8,
            "escrow": 81_200_000.0,
            "target_buy": 10_150_000.0,
            "target_sell": 11_800_000.0,
        },
    ]

    monkeypatch.setattr(oa, "get_annotated_desks", lambda: ([], tac_rows, []))
    monkeypatch.setattr(
        oa,
        "load_cached_orders",
        lambda: {"buys": {"4001": {"price": 10_150_000.0, "volume_remain": 8, "volume_total": 9, "escrow": 81_200_000.0}}, "sells": {}},
    )
    monkeypatch.setattr(oa, "load_market_prices_cache", lambda: {"4001": {"top_buy": 10_300_000.0, "lowest_ask": 12_000_000.0}})
    monkeypatch.setattr(oa, "load_active_blacklist", lambda: (set(), set(), {}))
    monkeypatch.setattr(oa, "load_cached_assets", lambda max_age_seconds=600: [{"type_id": 4001, "quantity": 1}])

    res = oa.get_order_actions(desk="all", refresh_live=False)
    assert res["cancel_count"] == 1
    assert res["cancels"][0]["type_id"] == 4001
    assert "PARTIAL STUB" in res["cancels"][0]["reason"]


def test_missing_buys_filters_low_capital_and_low_profit_dust_orders(monkeypatch):
    import workflows.buy_orders as bo

    unplaced_rows = [
        {
            "type_id": 5001,
            "name": "Shadow Serpentis 1MN Afterburner",
            "desk_type": "evergreen",
            "char_status": "UNPLACED",
            "qty": 1,
            "target_buy": 24_720_000.0,
            "target_sell": 28_500_000.0,
        },
        {
            "type_id": 5002,
            "name": "Cruise Missile Specialization",
            "desk_type": "tactical",
            "char_status": "UNPLACED",
            "qty": 2,
            "target_buy": 2_710_000.0,
            "target_sell": 6_400_000.0,
        },
        {
            "type_id": 5003,
            "name": "Core X-Type EM Armor Hardener",
            "desk_type": "tactical",
            "char_status": "UNPLACED",
            "qty": 10,
            "target_buy": 146_300_000.0,
            "target_sell": 176_000_000.0,
        },
    ]

    books = {
        5001: (5001, 24_710_000.0, 28_500_000.0),
        5002: (5002, 2_700_000.0, 6_400_000.0),
        5003: (5003, 146_200_000.0, 176_000_000.0),
    }

    monkeypatch.setattr(bo, "get_annotated_desks", lambda: (unplaced_rows[:1], unplaced_rows[1:], []))
    monkeypatch.setattr(bo, "fetch_station_book_top", lambda tid: books[tid])

    res = bo.audit_missing_buys(desk="all")
    assert res["total_items"] == 1
    assert res["skipped_small_count"] == 2
    assert res["items"][0]["type_id"] == 5003


def test_missing_buys_uses_hold_recovery_target_and_skips_buried_margin_caps(monkeypatch):
    import workflows.buy_orders as bo

    unplaced_rows = [
        {
            "type_id": 6001,
            "name": "Medium Vorton Projector II",
            "desk_type": "hold",
            "char_status": "UNPLACED",
            "qty": 2,
            "target_buy": 144_000_000.0,
            "target_sell": 188_100_000.0,
        },
        {
            "type_id": 6002,
            "name": "Buried Margin Cap Module",
            "desk_type": "evergreen",
            "char_status": "UNPLACED",
            "qty": 2,
            "target_buy": 135_000_000.0,
            "target_sell": 150_600_000.0,
        },
    ]

    books = {
        6001: (6001, 145_100_000.0, 150_600_000.0),
        6002: (6002, 145_100_000.0, 150_600_000.0),
    }

    monkeypatch.setattr(bo, "get_annotated_desks", lambda: (unplaced_rows[1:], [], unplaced_rows[:1]))
    monkeypatch.setattr(bo, "fetch_station_book_top", lambda tid: books[tid])

    res = bo.audit_missing_buys(desk="all")
    assert res["total_items"] == 1
    assert res["skipped_small_count"] == 1
    assert res["items"][0]["type_id"] == 6001
    assert res["items"][0]["recommended_buy"] == 145_200_000.0
    assert res["items"][0]["squeezed"] is False

def test_performance_item_name_resolution_diagnostic(monkeypatch, tmp_path):
    import services.sde as sde_mod

    # 1. Check raw SDE resolution for sample IDs from your performance table
    sample_ids = [11558, 13920, 13924, 13960]
    resolved = sde_mod.resolve_type_ids(sample_ids)
    print("resolve_type_ids result:", resolved)

    assert resolved, (
        "resolve_type_ids returned empty; check open_analytics_connection(read_only=True, attach_sde=True) "
        "and whether sde.invTypes is attached/populated."
    )
    for tid in sample_ids:
        assert tid in resolved and resolved[tid] and not str(resolved[tid]).startswith("Type "), (
            f"Missing or fallback name for type_id={tid}: {resolved.get(tid)!r}"
        )

def test_book_depth_stats_distinguish_thin_lowball_from_saturation_wall():
    thin = compute_book_depth_stats([(140_000_000.0, 1), (175_000_000.0, 20)], daily_vol_30d=12.0)
    assert thin["is_thin_lowball"] is True
    assert thin["effective_clearing_ask"] == 175_000_000.0
    assert thin["ask_days_of_supply"] < 0.35

    wall = compute_book_depth_stats([(150_600_000.0, 2), (151_000_000.0, 50)], daily_vol_30d=25.0)
    assert wall["is_thin_lowball"] is False
    assert wall["effective_clearing_ask"] == 151_000_000.0
    assert wall["ask_days_of_supply"] >= 2.0