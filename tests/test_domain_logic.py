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
    import services.ledgers as led_mod

    cache_out = tmp_path / "market_cache.json"
    monkeypatch.setattr(mkt_mod, "MARKET_CACHE_FILE", str(cache_out))
    monkeypatch.setattr(led_mod, "load_desk_csv", lambda fname: [{"type_id": 10}, {"type_id": 20}, {"type_id": 30}] if "evergreen" in fname else [])
    monkeypatch.setattr(led_mod, "load_cached_orders", lambda: {"buys": {"10": {"price": 90.0}}, "sells": {"20": {"price": 110.0}}})
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
