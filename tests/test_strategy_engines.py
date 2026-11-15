import pandas as pd
from services.ledgers import (
    resolve_desk_incumbents,
    build_universe_exclusion_mask,
    select_desk_candidates,
    compute_candidate_pricing,
    compute_line_sizing,
)
from services.market import (
    _resolve_collision_strategy,
    _resolve_unlisted_strategy,
    evaluate_deal_opportunity,
)
from services.importer import (
    calc_hisec_single_contract,
    calc_hisec_optimal_split,
    suggest_courier_bundles,
)


def test_ledger_selection_pricing_and_sticky_incumbents():
    df = pd.DataFrame([
        {
            "type_id": 101,
            "item_name": "Gila",
            "group_name": "Cruiser",
            "category_name": "Ship",
            "total_history_days": 120,
            "price_m_180d": 260.0,
            "price_m_30d": 250.0,
            "price_m_7d": 248.0,
            "p20_low_m_30d": 230.0,
            "daily_vol_30d": 40.0,
            "dump_freq_pct": 55.0,
            "avg_spread_30d": 14.0,
            "daily_turnover_m": 10_000.0,
        },
        {
            "type_id": 102,
            "item_name": "Tetrimon Booster",
            "group_name": "Booster",
            "category_name": "Implant",
            "total_history_days": 60,
            "price_m_180d": 50.0,
            "price_m_30d": 50.0,
            "price_m_7d": 49.0,
            "p20_low_m_30d": 45.0,
            "daily_vol_30d": 100.0,
            "dump_freq_pct": 80.0,
            "avg_spread_30d": 25.0,
            "daily_turnover_m": 5_000.0,
        },
    ])

    incumbents = resolve_desk_incumbents(
        df=df,
        desk_type="tactical",
        claimed_ids=set(),
        active_buys={101: {"price": 225_000_000.0, "volume_remain": 3}},
        active_sells={},
        asset_qtys={},
        prior_desks={101: "tactical"},
    )
    assert 101 in incumbents.desk_sticky_ids

    mask = build_universe_exclusion_mask(
        df=df,
        claimed_ids=set(),
        incumbents=incumbents,
        bl_items=set(),
        bl_groups=set(),
        bl_meta={"Tetrimon": {"type": "pattern"}},
    )
    assert list(df[mask]["type_id"]) == [101]

    candidates, params = select_desk_candidates(df, "tactical", mask, incumbents.desk_sticky_ids)
    assert len(candidates) == 1

    row = candidates.iloc[0]
    pricing = compute_candidate_pricing(row, "tactical", {"lowest_ask": 265_000_000.0, "top_buy": 225_000_000.0}, incumbents, True)
    assert pricing is not None
    assert pricing["target_buy"] == 225_000_000.0
    assert pricing["net_margin"] > 7.5

    sizing = compute_line_sizing(row, pricing, params, incumbents, is_sticky=True, passes_desk=True, remaining_budget=5_000_000_000.0)
    assert sizing is not None
    assert sizing["qty"] >= 3


def test_advisor_collision_and_exit_strategies():
    # Tactical <20% active ratio -> CANCEL & MERGE
    action, act_type, rec_sell, col = _resolve_collision_strategy(
        desk="TACTICAL",
        qty=9,
        unit_cost=100_000_000.0,
        lowest_ask=120_000_000.0,
        active_orders=[{"price": 125_000_000.0, "volume_remain": 1}],
        hold_meta={},
    )
    assert action == "CANCEL & MERGE"
    assert col["action"] == "CANCEL_MERGE"
    assert rec_sell == 119_999_999.99

    # Tactical >=20% active ratio -> MODIFY & HOLD
    action2, act_type2, _, col2 = _resolve_collision_strategy(
        desk="TACTICAL",
        qty=2,
        unit_cost=100_000_000.0,
        lowest_ask=120_000_000.0,
        active_orders=[{"price": 125_000_000.0, "volume_remain": 3}],
        hold_meta={},
    )
    assert action2 == "MODIFY & HOLD"
    assert col2["action"] == "MODIFY_HOLD"

    # Hold desk expired -> TIME STOP
    u_act, u_type, _ = _resolve_unlisted_strategy(
        desk="HOLD",
        unit_cost=100_000_000.0,
        lowest_ask=108_000_000.0,
        target_sell=135_000_000.0,
        hold_meta={"days_held": 22, "is_expired": True},
    )
    assert u_type == "time_stop"
    assert "TIME STOP" in u_act


def test_deal_sniper_archetypes_and_cluster_guard():
    meta = {"type_id": 500, "name": "Dread Guristas Shield Booster", "median_30d": 300_000_000.0}

    # Instant arb when lowest ask <= top buy
    arb = evaluate_deal_opportunity(meta, sells=[(200_000_000.0, 2), (295_000_000.0, 5)], top_buy=230_000_000.0)
    assert arb is not None and arb["deal_type"] == "INSTANT ARB"

    # Firesale dump when isolated underbid sits well below wall & 30d median
    dump = evaluate_deal_opportunity(meta, sells=[(210_000_000.0, 1), (298_000_000.0, 4)], top_buy=180_000_000.0)
    assert dump is not None and dump["deal_type"] == "FIRESALE DUMP"

    # Stacked cluster (>3 units and >300M ISK within 3.5%) must be rejected
    stacked = evaluate_deal_opportunity(meta, sells=[(210_000_000.0, 4), (298_000_000.0, 4)], top_buy=180_000_000.0)
    assert stacked is None


def test_hisec_freight_splitting_and_jf_bundling():
    tier, reward = calc_hisec_single_contract(m3=10_000.0, collateral=1_400_000_000.0, warps=35)
    assert tier == "Small (DST)"
    assert reward == 52_500_000.0

    # 2.8B collateral splits into 2x <=1.5B DST contracts (105M) instead of 1x 3B multiplier (157.5M)
    split = calc_hisec_optimal_split(total_m3=20_000.0, total_collateral=2_800_000_000.0, warps=35)
    assert split["contracts"] == 2
    assert split["total_reward"] == 105_000_000.0

    # Jump Freighter bundling respects 350,000 m3 max volume per contract
    bundles = suggest_courier_bundles(
        dest_key="cj6mt",
        items=[{"type_id": 27, "name": "Rattlesnake", "qty": 10, "unit_m3": 50_000.0, "unit_price": 750_000_000.0}],
    )
    assert bundles["contract_count"] == 2
    assert all(b["total_m3"] <= 350_000.0 for b in bundles["bundles"])
