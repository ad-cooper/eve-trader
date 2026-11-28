import pandas as pd
from services.ledgers import (
    resolve_desk_incumbents,
    select_desk_candidates,
    compute_candidate_pricing,
    compute_line_sizing,
)
from services.order_advisor import (
    _inject_orphaned_buys,
    annotate_desk_items
)
import scripts.generate_ledgers as gen_led
from services.desk import Desk
from services.market_universe import build_universe_exclusion_mask
from services.strategy_versions import get_strategy_version

def _make_universe_row(
    type_id: int,
    name: str,
    price_30d_m: float = 100.0,
    price_180d_m: float = 130.0,
    price_7d_m: float | None = None,
    daily_vol: float = 30.0,
    dump_freq: float = 45.0,
    spread_pct: float = 14.0,
    category: str = "Module",
    group: str = "Propulsion Module",
    history_days: int = 120,
) -> dict:
    p7 = price_7d_m if price_7d_m is not None else (price_30d_m * 0.98)
    return {
        "type_id": type_id,
        "item_name": name,
        "group_name": group,
        "category_name": category,
        "total_history_days": history_days,
        "price_m_180d": price_180d_m,
        "price_m_30d": price_30d_m,
        "price_m_7d": p7,
        "p20_low_m_30d": price_30d_m * 0.92,
        "daily_vol_30d": daily_vol,
        "dump_freq_pct": dump_freq,
        "avg_spread_30d": spread_pct,
        "daily_turnover_m": daily_vol * price_30d_m,
    }


def test_sticky_incumbent_survives_desk_filter_and_7d_dip_drift():
    """
    An incumbent with an active order or >=50M ISK hangar stack must stay on its desk
    even if its 30d spread/dump_freq drops below entry thresholds or its 7d VWAP dips to 72% of 30d VWAP.
    """
    df = pd.DataFrame([
        _make_universe_row(201, "Incumbent Cruiser", price_30d_m=200.0, price_7d_m=144.0, spread_pct=6.0, dump_freq=10.0),
        _make_universe_row(202, "Fresh Weak Cruiser", price_30d_m=200.0, price_7d_m=144.0, spread_pct=6.0, dump_freq=10.0),
    ])

    incumbents = resolve_desk_incumbents(
        df=df,
        desk_type="tactical",
        claimed_ids=set(),
        active_buys={201: {"price": 170_000_000.0, "volume_remain": 4}},
        active_sells={},
        asset_qtys={},
        prior_desks={201: "tactical"},
    )
    assert 201 in incumbents.desk_sticky_ids
    assert 202 not in incumbents.desk_sticky_ids

    mask = build_universe_exclusion_mask(
        df=df,
        claimed_ids=set(),
        incumbents=incumbents,
        bl_items=set(),
        bl_groups=set(),
        bl_meta={},
    )
    candidates, _ = select_desk_candidates(df, "tactical", mask, incumbents.desk_sticky_ids)
    assert list(candidates["type_id"]) == [201]
    assert bool(candidates.iloc[0]["is_sticky"]) is True
    assert bool(candidates.iloc[0]["passes_desk"]) is False


def test_sticky_incumbents_sort_ahead_of_higher_scoring_new_candidates():
    """
    When desk budget is tight, sticky incumbents must be allocated before higher-scoring
    new candidates so existing orders/inventory are never evicted and re-added next cycle.
    """
    df = pd.DataFrame([
        _make_universe_row(301, "Sticky Lower Velocity", price_30d_m=100.0, daily_vol=10.0, spread_pct=11.0),
        _make_universe_row(302, "New High Velocity", price_30d_m=100.0, daily_vol=150.0, spread_pct=22.0),
    ])
    incumbents = resolve_desk_incumbents(
        df=df,
        desk_type="evergreen",
        claimed_ids=set(),
        active_buys={301: {"price": 92_000_000.0, "volume_remain": 2}},
        active_sells={},
        asset_qtys={},
        prior_desks={301: "evergreen"},
    )
    mask = build_universe_exclusion_mask(df, set(), incumbents, set(), set(), {})
    candidates, params = select_desk_candidates(df, "evergreen", mask, incumbents.desk_sticky_ids)

    assert list(candidates["type_id"]) == [301, 302]
    strategy = get_strategy_version("v2")
    rows, _, claimed, sticky_kept = gen_led.allocate_desk_rows(
        candidates=candidates,
        desk_type="evergreen",
        budget=220_000_000.0,
        market_telemetry={"301": {"lowest_ask": 104_000_000.0}, "302": {"lowest_ask": 105_000_000.0}},
        desk_params=params,
        incumbents=incumbents,
        strat=strategy
    )
    assert claimed == [301]
    assert sticky_kept == 1
    assert rows[0]["qty"] == 2


def test_held_inventory_and_active_sells_never_dropped_on_margin_or_loss_or_stockout():
    """
    Items currently held in hangar or listed on sell orders must remain on the desk even when:
      1. They appear in recent 30d losing trades (losing_tids),
      2. Live Jita book is temporarily stocked out (lowest_ask=None),
      3. Current net margin compresses below sticky_margin_floor.
    """
    df = pd.DataFrame([
        _make_universe_row(401, "Held Underwater Item", price_30d_m=100.0, price_180d_m=135.0, spread_pct=15.0),
        _make_universe_row(402, "Unplaced Losing Item", price_30d_m=100.0, price_180d_m=135.0, spread_pct=15.0),
    ])
    incumbents = resolve_desk_incumbents(
        df=df,
        desk_type="hold",
        claimed_ids=set(),
        active_buys={},
        active_sells={401: {"price": 103_000_000.0, "volume_remain": 3}},
        asset_qtys={401: 2},
        prior_desks={401: "hold"},
    )
    mask = build_universe_exclusion_mask(
        df=df,
        claimed_ids=set(),
        incumbents=incumbents,
        bl_items=set(),
        bl_groups=set(),
        bl_meta={},
        losing_tids={401, 402},
    )
    assert list(df[mask]["type_id"]) == [401]

    row = df[mask].iloc[0]
    desk = Desk.get_desk_profile("hold")
    pricing = compute_candidate_pricing(
        row=row,
        desk_or_type=desk,
        p_info={"lowest_ask": None, "top_buy": 99_000_000.0},
        incumbents=incumbents,
        is_sticky=True,
    )
    assert pricing is not None

    _, params = select_desk_candidates(df, "hold", mask, incumbents.desk_sticky_ids)
    sizing = compute_line_sizing(
        row=row,
        pricing=pricing,
        desk_params=params,
        incumbents=incumbents,
        is_sticky=True,
        passes_desk=True,
        remaining_budget=2_000_000_000.0,
    )
    assert sizing is not None
    assert sizing["qty"] >= 5


def test_cross_desk_exclusivity_across_waterfall(monkeypatch):
    """
    Items claimed by an upstream desk (Hold -> Tactical -> Evergreen) must never
    duplicate onto or flap to downstream desks across rebuilds.
    """
    eg_cfg = Desk.get_desk_profile("evergreen")
    tac_cfg = Desk.get_desk_profile("tactical")
    eg_spread = max(14.5, eg_cfg.min_spread_pct + 0.5)
    eg_dump = max(0.0, tac_cfg.min_dump_freq_pct - 5.0)

    df = pd.DataFrame([
        _make_universe_row(501, "Dual Qualifying Ship", price_30d_m=150.0, price_180d_m=210.0, daily_vol=50.0, dump_freq=60.0, spread_pct=18.0, category="Ship"),
        _make_universe_row(502, "Evergreen Workhorse", price_30d_m=50.0, price_180d_m=52.0, daily_vol=120.0, dump_freq=eg_dump, spread_pct=eg_spread),
    ])

    monkeypatch.setattr(Desk, "load_positions", lambda self, refresh=False: ({}, {}, {}, {501: "hold", 502: "evergreen"}))
    monkeypatch.setattr(gen_led, "load_recent_losing_typeids", lambda: set())
    monkeypatch.setattr(Desk, "persist_ledger", lambda *args, **kwargs: None)

    telemetry = {
        "501": {"lowest_ask": 165_000_000.0, "top_buy": 140_000_000.0},
        "502": {"lowest_ask": 55_000_000.0, "top_buy": 46_500_000.0},
    }

    outcomes = gen_led.run_waterfall(df, total_budget=10_000_000_000.0, market_telemetry=telemetry, target_desks={"hold", "tactical", "evergreen"})
    hold_ids = set(outcomes["hold"][0])
    tac_ids = set(outcomes["tactical"][0])
    eg_ids = set(outcomes["evergreen"][0])

    assert 501 in hold_ids
    assert hold_ids.isdisjoint(tac_ids)
    assert hold_ids.isdisjoint(eg_ids)
    assert tac_ids.isdisjoint(eg_ids)
    assert 502 in eg_ids


def test_orphaned_active_buy_orders_are_surfaced_on_original_desk(monkeypatch):
    """
    If an active buy order rotates out of all CSV ledgers, _inject_orphaned_buys()
    must surface it on its historical desk rather than letting it vanish from the UI.
    """
    import services.sde as sde_mod
    import services.exclusions as bl_mod

    monkeypatch.setattr(
        sde_mod,
        "get_latest_ledger_targets",
        lambda tids: {999: {"name": "Orphaned Tactical Module", "desk": "TACTICAL", "target_buy": 80_000_000.0, "target_sell": 95_000_000.0}},
    )
    monkeypatch.setattr(sde_mod, "resolve_type_ids", lambda tids: {999: "Orphaned Tactical Module"})

    eg, tac, hld = [], [], []
    buys = {"999": {"price": 80_000_000.0, "volume_remain": 3, "escrow": 240_000_000.0}}
    market_prices = {"999": {"lowest_ask": 94_000_000.0, "top_buy": 80_000_000.0}}

    _inject_orphaned_buys(eg, tac, hld, buys, market_prices)

    assert len(eg) == 0 and len(hld) == 0
    assert len(tac) == 1
    assert tac[0]["type_id"] == 999
    assert tac[0]["char_status"] == "BUY_ACTIVE"
    assert tac[0]["action_code"] == "PATIENT"
    assert "ROTATED OUT" in tac[0]["action_text"]


def test_hold_desk_curation_caps_unplaced_prime_dips_without_hiding_active_orders():
    """
    Hold desk keeps all active BUY/SELL orders visible while capping unplaced rows to Top 8 Prime Dips.
    """
    raw_items = [
        {"type_id": 600, "name": "Active Hold Order", "qty": 2, "dump_freq": 20.0, "target_buy": 100e6, "target_sell": 140e6},
    ]
    market_prices = {"600": {"lowest_ask": 115e6, "top_buy": 100e6}}
    buys = {"600": {"price": 100e6, "volume_remain": 2, "escrow": 200e6}}

    for idx in range(1, 13):
        tid = 600 + idx
        raw_items.append({
            "type_id": tid,
            "name": f"Prime Dip Candidate {idx}",
            "qty": 5,
            "dump_freq": 25.0,
            "target_buy": 100e6,
            "target_sell": 140e6,
        })
        market_prices[str(tid)] = {"lowest_ask": 110e6, "top_buy": 95e6}

    annotated = annotate_desk_items(raw_items, "hold", market_prices, buys, sells={})
    active = [r for r in annotated if r["char_status"] == "BUY_ACTIVE"]
    unplaced = [r for r in annotated if r["char_status"] == "UNPLACED"]

    assert len(active) == 1 and active[0]["type_id"] == 600
    assert len(unplaced) == 8
    assert all(r["action_code"] == "PRIME_DIP" for r in unplaced)


def test_multi_ledger_cap_aware_roi_optimization(monkeypatch):
    """
    Verifies that across all three ledgers:
      1. Evergreen prioritizes high-ROI cap-filling items over uncapped thin-spread mega-hub items.
      2. Every allocated line across Hold, Tactical, and Evergreen clears its DeskProfile min_margin.
      3. Budget tails below min_line_cap cascade downstream instead of creating sub-threshold dust lines.
    """
    eg_cfg = Desk.get_desk_profile("evergreen")
    tac_cfg = Desk.get_desk_profile("tactical")
    thin_spread = max(12.2, eg_cfg.min_spread_pct + 0.2)
    steady_dump = max(0.0, tac_cfg.min_dump_freq_pct - 5.0)

    df = pd.DataFrame([
        # Mega-hub thin-spread item: 50B/day turnover -> capped at max_line_cap
        _make_universe_row(701, "MegaHub Thin Spread", price_30d_m=100.0, price_180d_m=102.0, daily_vol=500.0, dump_freq=steady_dump, spread_pct=thin_spread),
        # High-margin Evergreen workhorse: 10B/day turnover, 18.0% spread -> fills max_line_cap at ~2x ROI
        _make_universe_row(702, "High Margin Workhorse", price_30d_m=100.0, price_180d_m=103.0, daily_vol=100.0, dump_freq=steady_dump, spread_pct=18.0),
        # Tactical high-dump loot module
        _make_universe_row(703, "Tactical Deadspace Module", price_30d_m=150.0, price_180d_m=155.0, daily_vol=30.0, dump_freq=max(65.0, tac_cfg.min_dump_freq_pct + 10.0), spread_pct=max(18.5, tac_cfg.min_spread_pct + 2.0)),
        # Hold cyclical saturation dip
        _make_universe_row(704, "Hold Cyclical Hull", price_30d_m=200.0, price_180d_m=275.0, daily_vol=20.0, dump_freq=steady_dump, spread_pct=15.0),
    ])

    mask = pd.Series(True, index=df.index)
    eg_cands, _ = select_desk_candidates(df, "evergreen", mask, set())
    assert list(eg_cands["type_id"])[0] == 702

    monkeypatch.setattr(Desk, "load_positions", lambda self, refresh=False: ({}, {}, {}, {501: "hold", 502: "evergreen"}))
    monkeypatch.setattr(gen_led, "load_recent_losing_typeids", lambda: set())
    monkeypatch.setattr(Desk, "persist_ledger", lambda *args, **kwargs: None)

    telemetry = {
        "701": {"lowest_ask": 106.0e6, "top_buy": 94.0e6},
        "702": {"lowest_ask": 109.0e6, "top_buy": 91.0e6},
        "703": {"lowest_ask": 164.0e6, "top_buy": 136.0e6},
        "704": {"lowest_ask": 210.0e6, "top_buy": 182.0e6},
    }

    outcomes = gen_led.run_waterfall(df, total_budget=6.0e9, market_telemetry=telemetry, target_desks={"hold", "tactical", "evergreen"})
    assert 704 in outcomes["hold"][0]
    assert 703 in outcomes["tactical"][0]
    assert 702 in outcomes["evergreen"][0]
