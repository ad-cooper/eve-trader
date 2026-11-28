import datetime
import duckdb
import pandas as pd
import scripts.backtest_replay as replay


def test_causal_trade_trajectory_prevents_prefill_spike_and_handles_timestop():
    # Day 1 spike above target_sell happens BEFORE Day 3 fill -> must NOT falsely count as Day 1 exit
    bars = [
        {"lowest": 105.0, "highest": 135.0, "average": 115.0, "volume": 20},
        {"lowest": 102.0, "highest": 110.0, "average": 106.0, "volume": 20},
        {"lowest": 98.0,  "highest": 104.0, "average": 101.0, "volume": 20},
        {"lowest": 103.0, "highest": 116.0, "average": 110.0, "volume": 20},
    ]
    res = replay.simulate_trade_trajectory(bars, qty=4, target_buy=100.0, target_sell=115.0, max_hold_days=10)
    assert res["status"] == "TARGET_HIT"
    assert res["fill_day"] == 3
    assert res["exit_day"] == 4
    assert res["filled_qty"] == 4
    assert res["realized_pnl"] > 0

    # Price drops continuously after fill -> exits at TIME_STOP on day 4
    down_bars = [
        {"lowest": 95.0, "highest": 99.0, "average": 97.0, "volume": 50},
        {"lowest": 90.0, "highest": 94.0, "average": 92.0, "volume": 50},
        {"lowest": 88.0, "highest": 91.0, "average": 89.0, "volume": 50},
        {"lowest": 85.0, "highest": 88.0, "average": 86.0, "volume": 50},
    ]
    ts_res = replay.simulate_trade_trajectory(down_bars, qty=5, target_buy=100.0, target_sell=115.0, max_hold_days=3)
    assert ts_res["status"] == "TIME_STOP"
    assert ts_res["exit_day"] == 4
    assert ts_res["realized_pnl"] < 0


def test_end_to_end_point_in_time_replay_and_missed_winner_audit(tmp_path, monkeypatch):
    db_path = tmp_path / "replay_test.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("""
        CREATE TABLE forge_market_history (
            date DATE,
            type_id INTEGER,
            average DOUBLE,
            highest DOUBLE,
            lowest DOUBLE,
            order_count INTEGER,
            volume BIGINT
        );
    """)

    rows = []
    start = datetime.date(2026, 9, 1)
    for d in range(75):
        cur_date = start + datetime.timedelta(days=d)
        if d < 40:
            p901 = 225_000_000.0
        elif d <= 60:
            p901 = 165_000_000.0
        else:
            p901 = 215_000_000.0
        rows.append((cur_date, 901, p901, p901 * 1.10, p901 * 0.90, 20, 25))

        if d < 30:
            rows.append((cur_date, 902, 230_000_000.0, 253_000_000.0, 220_800_000.0, 18, 50))
        elif d <= 60:
            rows.append((cur_date, 902, 192_000_000.0, 211_200_000.0, 184_320_000.0, 18, 50))
        elif d <= 62:
            rows.append((cur_date, 902, 184_000_000.0, 190_000_000.0, 174_000_000.0, 18, 50))
        else:
            rows.append((cur_date, 902, 240_000_000.0, 248_000_000.0, 232_000_000.0, 18, 50))

    con.executemany("INSERT INTO forge_market_history VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    sde_meta = pd.DataFrame([
        {"type_id": 901, "item_name": "Pith X-Type EM Shield Hardener", "group_name": "Shield Hardener", "category_name": "Module"},
        {"type_id": 902, "item_name": "Gist X-Type Large Shield Booster", "group_name": "Shield Booster", "category_name": "Module"},
    ])

    monkeypatch.setattr(replay, "load_active_blacklist", lambda: (set(), set(), {}))

    report = replay.run_walkforward_replay(
        con=con,
        sde_meta_df=sde_meta,
        days_back=20,
        step_days=5,
        horizon_days=12,
        budget=10.0e9,
        sqlite_path="",
        duckdb_path=str(db_path),
    )
    con.close()

    df_trades = report["trades"]
    assert not df_trades.empty
    assert {"desk_type", "type_id", "supply_class", "realized_pnl"}.issubset(df_trades.columns)
    assert set(df_trades["supply_class"]).issubset({"FARMED_RNG", "LP_FACTION", "MANUFACTURED"})
    assert 901 in set(df_trades["type_id"])

    missed_902 = [m for m in report["missed"] if m["type_id"] == 902]
    assert len(missed_902) >= 1
    assert missed_902[0]["blocker"] == "DISCOUNT_TO_180D_TOO_SHALLOW"
