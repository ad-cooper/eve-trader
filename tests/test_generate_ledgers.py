import importlib.util
import pandas
import sqlite3
from pathlib import Path
from services.strategy_versions import enrich_universe_fundamentals

def test_resolve_dynamic_budget_explicit_auto_and_fallback(monkeypatch):
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "generate_ledgers.py"
    spec = importlib.util.spec_from_file_location("generate_ledgers", script_path)
    gl_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gl_mod)

    # 1. Explicit number (< 1000 -> billions, >= 1000 -> raw ISK)
    assert gl_mod.resolve_dynamic_budget("75.0") == 75.0e9
    assert gl_mod.resolve_dynamic_budget("25000000000") == 25_000_000_000.0

    # 2. 'auto' uses resolved equity when above 1B ISK
    monkeypatch.setattr(gl_mod, "resolve_character_equity", lambda **kw: (30e9, 15e9, 45e9))
    assert gl_mod.resolve_dynamic_budget("auto") == 45e9

    # 3. Invalid capital falls back to 'auto' resolution
    assert gl_mod.resolve_dynamic_budget("not-a-number") == 45e9

    # 4. Low/zero equity falls back to DEFAULT_FALLBACK_BUDGET
    monkeypatch.setattr(gl_mod, "resolve_character_equity", lambda **kw: (0.0, 0.0, 500e6))
    assert gl_mod.resolve_dynamic_budget("auto") == gl_mod.DEFAULT_FALLBACK_BUDGET

def _sample_universe() -> pandas.DataFrame:
    return pandas.DataFrame([
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

def test_v2_rejects_ultra_low_volume_high_price_traps():
    from services.strategy_versions import v2_select_desk_candidates

    df = pandas.DataFrame([
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
    mask = pandas.Series(True, index=df.index)
    cands, _ = v2_select_desk_candidates(df, "tactical", mask, set())
    picked_ids = set(cands["type_id"].astype(int))
    assert 99901 not in picked_ids
    assert 99902 in picked_ids
