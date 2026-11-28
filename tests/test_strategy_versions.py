import os
import sqlite3
import pandas as pd
import scripts.generate_ledgers as gen_led
from services.strategy_versions import (
    StrategyVersion,
    register_strategy_version,
    get_strategy_version,
    list_strategy_versions,
    get_active_strategy_version,
    set_active_strategy_version,
    get_version_output_dir,
    compute_book_depth_stats,
    enrich_universe_fundamentals,
    v2_evaluate_order_action,
    v2_evaluate_sell_collision,
)
from services.desk import Desk

def _sample_universe() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "type_id": 801,
            "item_name": "Caracal",
            "group_name": "Cruiser",
            "category_name": "Ship",
            "total_history_days": 180,
            "price_m_180d": 12.0,
            "price_m_30d": 11.8,
            "price_m_7d": 11.7,
            "p20_low_m_30d": 10.8,
            "daily_vol_30d": 120.0,
            "dump_freq_pct": 18.0,
            "avg_spread_30d": 14.0,
            "daily_turnover_m": 1416.0,
        },
        {
            "type_id": 802,
            "item_name": "Imperial Navy Energized Adaptive Nano Membrane",
            "group_name": "Armor Coating",
            "category_name": "Module",
            "total_history_days": 180,
            "price_m_180d": 85.0,
            "price_m_30d": 82.0,
            "price_m_7d": 80.0,
            "p20_low_m_30d": 72.0,
            "daily_vol_30d": 35.0,
            "dump_freq_pct": 55.0,
            "avg_spread_30d": 18.5,
            "daily_turnover_m": 2870.0,
        },
        {
            "type_id": 803,
            "item_name": "Pith X-Type EM Shield Hardener",
            "group_name": "Shield Hardener",
            "category_name": "Module",
            "total_history_days": 180,
            "price_m_180d": 220.0,
            "price_m_30d": 165.0,
            "price_m_7d": 160.0,
            "p20_low_m_30d": 150.0,
            "daily_vol_30d": 12.0,
            "dump_freq_pct": 40.0,
            "avg_spread_30d": 19.0,
            "daily_turnover_m": 1980.0,
        },
    ])


def test_sde_supply_elasticity_classification(tmp_path):
    sde_path = tmp_path / "sde.db"
    con = sqlite3.connect(str(sde_path))
    con.executescript("""
        CREATE TABLE industryActivityProducts (typeID INTEGER, activityID INTEGER, productTypeID INTEGER, quantity INTEGER);
        CREATE TABLE invMetaTypes (typeID INTEGER, parentTypeID INTEGER, metaGroupID INTEGER);
        INSERT INTO industryActivityProducts VALUES (9001, 1, 801, 1);
        INSERT INTO invMetaTypes VALUES (802, 801, 4), (803, 801, 5);
    """)
    con.commit()
    con.close()

    enriched = enrich_universe_fundamentals(_sample_universe(), sqlite_path=str(sde_path))
    by_id = enriched.set_index("type_id").to_dict(orient="index")

    assert by_id[801]["supply_class"] == "MANUFACTURED"
    assert by_id[801]["natural_cycle_days"] == 3.0
    assert by_id[802]["supply_class"] == "LP_FACTION"
    assert by_id[802]["natural_cycle_days"] == 8.0
    assert by_id[803]["supply_class"] == "FARMED_RNG"
    assert by_id[803]["natural_cycle_days"] == 21.0


def test_book_depth_stats_distinguish_thin_lowball_from_saturation_wall():
    thin = compute_book_depth_stats([(140_000_000.0, 1), (175_000_000.0, 20)], daily_vol_30d=12.0)
    assert thin["is_thin_lowball"] is True
    assert thin["effective_clearing_ask"] == 175_000_000.0
    assert thin["ask_days_of_supply"] < 0.35

    wall = compute_book_depth_stats([(150_600_000.0, 2), (151_000_000.0, 50)], daily_vol_30d=25.0)
    assert wall["is_thin_lowball"] is False
    assert wall["effective_clearing_ask"] == 151_000_000.0
    assert wall["ask_days_of_supply"] >= 2.0


def test_v2_holds_through_thin_lowballs_on_buys_and_sells():
    b_code, b_label = v2_evaluate_order_action(
        desk_type="tactical",
        my_price=145_000_000.0,
        top_buy=145_000_000.0,
        t_sell=148_000_000.0,
        dump_freq=45.0,
        csv_sell=175_000_000.0,
        live_ask=140_000_000.0,
        ask_days_of_supply=0.10,
        supply_class="FARMED_RNG",
    )
    assert b_code == "TOP_BOOK"
    assert "THIN SUPPLY" in b_label

    act, act_type, rec_sell, col = v2_evaluate_sell_collision(
        desk="TACTICAL",
        qty=3,
        unit_cost=135_000_000.0,
        lowest_ask=140_000_000.0,
        active_orders=[{"price": 175_000_000.0, "volume_remain": 4}],
        hold_meta={},
        ask_days_of_supply=0.10,
        supply_class="FARMED_RNG",
    )
    assert act == "HOLD WALL (THIN LOWBALL)"
    assert rec_sell == 175_000_000.0
    assert col["action"] == "HOLD_HANGAR"


def test_side_by_side_v1_and_v2_shadow_isolation_and_ai_registration(tmp_path, monkeypatch):
    monkeypatch.setattr("services.strategy_versions.STRATEGY_CONFIG_FILE", str(tmp_path / "strategy_config.json"))
    monkeypatch.setattr(gen_led, "OUTPUT_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setattr(gen_led, "SQLITE_PATH", "")
    monkeypatch.setattr(Desk, "load_positions", lambda self, refresh=False: ({}, {}, {}, {}))
    monkeypatch.setattr(gen_led, "load_recent_losing_typeids", lambda: set())

    def _mock_persist_ledger(
        self,
        ledger_rows,
        allocated_cap,
        budget,
        sticky_kept,
        snapshot_time=None,
    ):
        out_dir = get_version_output_dir(self.strategy_version, base_dir=gen_led.OUTPUT_DIR)
        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, f"{self.desk_type}_ledger.csv")
        pd.DataFrame(ledger_rows).to_csv(out_file, index=False)

    monkeypatch.setattr(Desk, "persist_ledger", _mock_persist_ledger)

    df = _sample_universe()
    telemetry = {
        "801": {"lowest_ask": 12.6e6, "top_buy": 10.8e6},
        "802": {"lowest_ask": 88.0e6, "top_buy": 74.0e6},
        "803": {"lowest_ask": 172.0e6, "top_buy": 148.0e6},
    }

    assert get_active_strategy_version() == "v2"
    gen_led.run_waterfall(df, total_budget=6.0e9, market_telemetry=telemetry, target_desks={"hold", "tactical", "evergreen"}, strategy_version="v1")
    gen_led.run_waterfall(df, total_budget=6.0e9, market_telemetry=telemetry, target_desks={"hold", "tactical", "evergreen"}, strategy_version="v2")

    assert os.path.exists(tmp_path / "ledgers" / "hold_ledger.csv")
    assert os.path.exists(tmp_path / "ledgers" / "shadow" / "v1" / "hold_ledger.csv")

    v2_spec = get_strategy_version("v2")
    register_strategy_version(
        StrategyVersion(
            version="ai-candidate-test",
            description="Experimental AI candidate",
            enrich_universe=v2_spec.enrich_universe,
            select_candidates=v2_spec.select_candidates,
            price_candidate=v2_spec.price_candidate,
            size_line=v2_spec.size_line,
            evaluate_buy_order=v2_spec.evaluate_buy_order,
            evaluate_sell_collision=v2_spec.evaluate_sell_collision,
        )
    )
    assert "ai-candidate-test" in list_strategy_versions()
    assert set_active_strategy_version("ai-candidate-test") == "ai-candidate-test"
    assert get_version_output_dir("ai-candidate-test", base_dir=str(tmp_path / "ledgers")) == str(tmp_path / "ledgers")
    assert get_version_output_dir("v1", base_dir=str(tmp_path / "ledgers")) == str(tmp_path / "ledgers" / "shadow" / "v1")


def test_persist_desk_ledger_logs_strategy_version_to_duckdb(tmp_path, monkeypatch):
    import duckdb
    import datetime

    db_path = tmp_path / "test_analytics.duckdb"
    monkeypatch.setattr(gen_led, "OUTPUT_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setattr(gen_led, "DUCKDB_PATH", str(db_path))
    monkeypatch.setattr("services.strategy_versions.STRATEGY_CONFIG_FILE", str(tmp_path / "strategy_config.json"))

    rows = [
        {
            "type_id": 801,
            "name": "Caracal",
            "qty": 5,
            "dump_freq": 18.0,
            "target_buy": 11_000_000.0,
            "target_sell": 12_600_000.0,
            "net_margin": 8.8,
            "capital": 55_275_000.0,
            "profit": 4_850_000.0,
            "supply_class": "MANUFACTURED",
        }
    ]
    ts = datetime.datetime(2026, 11, 24, 18, 0, 0, tzinfo=datetime.timezone.utc)
    desk = Desk(
        desk_type="evergreen",
        strategy_version="v2",
        output_dir=str(tmp_path / "ledgers"),
        db_path=str(db_path),
    )
    desk.persist_ledger(rows, 55_275_000.0, 1_000_000_000.0, 0, snapshot_time=ts)
    con = duckdb.connect(str(db_path), read_only=True)
    logged = con.execute("SELECT strategy_version, supply_class, type_id FROM ledger_history").fetchall()
    con.close()
    assert logged == [("v2", "MANUFACTURED", 801)]


def test_v2_rejects_ultra_low_volume_high_price_traps():
    from services.strategy_versions import v2_select_desk_candidates

    df = pd.DataFrame([
        {
            "type_id": 99901,
            "item_name": "Neophite",
            "group_name": "Commodities",
            "category_name": "Commodity",
            "total_history_days": 180,
            "active_days_30d": 7,
            "price_m_180d": 450.0,
            "price_m_30d": 420.0,
            "price_m_7d": 410.0,
            "p20_low_m_30d": 268.8,
            "daily_vol_30d": 0.35,
            "dump_freq_pct": 57.0,
            "avg_spread_30d": 48.0,
            "daily_turnover_m": 147.0,
        },
        {
            "type_id": 99902,
            "item_name": "Imperial Navy Large Armor Repairer",
            "group_name": "Armor Repair Unit",
            "category_name": "Module",
            "total_history_days": 180,
            "active_days_30d": 30,
            "price_m_180d": 265.0,
            "price_m_30d": 260.0,
            "price_m_7d": 258.0,
            "p20_low_m_30d": 227.5,
            "daily_vol_30d": 14.0,
            "dump_freq_pct": 42.0,
            "avg_spread_30d": 16.5,
            "daily_turnover_m": 3640.0,
        },
    ])
    mask = pd.Series(True, index=df.index)
    cands, _ = v2_select_desk_candidates(df, "tactical", mask, set())
    picked_ids = set(cands["type_id"].astype(int))
    assert 99901 not in picked_ids
    assert 99902 in picked_ids
