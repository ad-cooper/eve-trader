import datetime
import json
from contextlib import contextmanager
from pathlib import Path
import shutil

import duckdb
import pandas
import pytest

import constants
import services.analytics_db as analytics_db
import scripts.generate_ledgers as gen_led
import services.storage
import services.exclusions as bl_mod
import services.sde as sde_mod

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ledger_golden"


def _resolve_expected_csv(desk: str) -> Path:
    return FIXTURE_DIR / "expected" / "v2" / f"{desk}_ledger.csv"


def _assert_csv_matches_golden(actual_csv: Path, expected_csv: Path) -> None:
    assert expected_csv.exists(), f"Missing golden file: {expected_csv}"
    assert actual_csv.exists(), f"Pipeline did not produce output CSV: {actual_csv}"

    exp_df = pandas.read_csv(expected_csv)
    act_df = pandas.read_csv(actual_csv)

    assert list(act_df.columns) == list(exp_df.columns), (
        f"Column mismatch in {actual_csv.name}: {list(act_df.columns)} != {list(exp_df.columns)}"
    )
    assert len(act_df) == len(exp_df), (
        f"Row count mismatch in {actual_csv.name}: {len(act_df)} != {len(exp_df)}"
    )

    if exp_df.empty:
        return

    for col in exp_df.columns:
        if pandas.api.types.is_numeric_dtype(exp_df[col]):
            pandas.testing.assert_series_equal(
                act_df[col].astype("float64"),
                exp_df[col].astype("float64"),
                check_names=False,
                atol=1e-4,
                rtol=1e-6,
            )
        else:
            assert act_df[col].fillna("").astype(str).tolist() == exp_df[col].fillna("").astype(str).tolist(), (
                f"Mismatch in column '{col}' of {actual_csv.name}"
            )


def test_golden_ledger_waterfall_v2_invariant(tmp_path, monkeypatch):
    if not FIXTURE_DIR.exists():
        pytest.skip("Golden snapshot not exported yet; run scripts/export_golden_snapshot.py first.")

    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    snap_time = datetime.datetime.fromisoformat(manifest["snapshot_time"])
    budget = float(manifest["budget"])

    work_dir = tmp_path / "work"
    shutil.copytree(FIXTURE_DIR / "inputs", work_dir / "inputs")
    actual_out = tmp_path / "out" / "v2"
    actual_out.mkdir(parents=True, exist_ok=True)

    fixture_duck = work_dir / "inputs" / "fixture_analytics.duckdb"
    fixture_sqlite = work_dir / "inputs" / "fixture_sde.sqlite"
    temp_duck = work_dir / "run_analytics.duckdb"
    shutil.copy2(fixture_duck, temp_duck)

    @contextmanager
    def _open_analytics_connection(read_only: bool = False, attach_sde: bool = True, db_path=None, sde_path=None):
        con = duckdb.connect(str(temp_duck), read_only=read_only)
        try:
            if attach_sde and fixture_sqlite.exists():
                con.execute(f"ATTACH '{fixture_sqlite}' AS sde (TYPE SQLITE);")
            yield con
        finally:
            con.close()

    fixture_orders = json.loads((work_dir / "inputs" / "character_orders.json").read_text(encoding="utf-8"))
    fixture_assets = json.loads((work_dir / "inputs" / "character_assets.json").read_text(encoding="utf-8"))
    fixture_perf = json.loads((work_dir / "inputs" / "performance_cache.json").read_text(encoding="utf-8"))
    fixture_telemetry = json.loads((work_dir / "inputs" / "market_prices_cache.json").read_text(encoding="utf-8"))

    orig_read_json = services.storage.read_json

    def _fixture_read_json(path, default=None):
        p = str(path)
        if p.endswith("character_orders.json"):
            return fixture_orders
        if p.endswith("character_assets.json"):
            return fixture_assets
        if p.endswith("performance_cache.json"):
            return fixture_perf
        if p.endswith("market_prices_cache.json"):
            return fixture_telemetry
        return orig_read_json(path, default=default)

    monkeypatch.chdir(work_dir)
    monkeypatch.setattr(gen_led, "SQLITE_PATH", str(fixture_sqlite))
    monkeypatch.setattr(gen_led, "DUCKDB_PATH", str(temp_duck))
    monkeypatch.setattr(gen_led, "OUTPUT_DIR", str(actual_out))
    monkeypatch.setattr(constants, "MARKET_CACHE_FILE", str(work_dir / "inputs" / "market_prices_cache.json"))
    monkeypatch.setattr(analytics_db, "open_analytics_connection", _open_analytics_connection)
    monkeypatch.setattr(services.storage, "read_json", _fixture_read_json)
    monkeypatch.setattr(constants, "SQLITE_PATH", str(fixture_sqlite))
    monkeypatch.setattr(constants, "DUCKDB_PATH", str(temp_duck))
    monkeypatch.setattr(bl_mod, "BLACKLIST_FILE", str(work_dir / "inputs" / "temporary_blacklist.json"))
    monkeypatch.setattr("services.desk.read_json", _fixture_read_json)
    monkeypatch.setattr("services.strategy_versions.STRATEGY_CONFIG_FILE", str(work_dir / "strategy_config.json"))
    monkeypatch.setattr(
        "services.strategy_versions.get_version_output_dir",
        lambda ver, base_dir="ledgers": str(actual_out),
    )

    universe_df = gen_led.load_market_universe()

    gen_led.run_waterfall(
        df=universe_df,
        total_budget=budget,
        market_telemetry=fixture_telemetry,
        target_desks={"hold", "tactical", "evergreen"},
        strategy_version="v2",
        snapshot_time=snap_time,
    )

    for desk in manifest["desks"]:
        _assert_csv_matches_golden(
            actual_csv=actual_out / f"{desk}_ledger.csv",
            expected_csv=_resolve_expected_csv(desk),
        )