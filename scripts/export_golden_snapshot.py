#!/usr/bin/env python3
"""
Exports a compact, sanitized golden-master snapshot for ledger refactoring invariants.
Slices DuckDB & SQLite FIRST, switches DB paths/connections, then calls load_market_universe()
and run_waterfall() for v1 and v2.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contextlib import contextmanager
import services.analytics_db as analytics_db

import scripts.generate_ledgers as gen_led
import services.exclusions as bl_mod
import services.sde as sde_mod
import services.strategy_versions as strat_mod

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ledger_golden"
FIXED_SNAPSHOT = datetime.datetime(2026, 11, 26, 11, 30, 0, tzinfo=datetime.timezone.utc)
GOLDEN_BUDGET = 50.0e9

HOLD_LEDGER_IDS = [
    57444, 52244, 53299, 85662, 57442, 90469, 81948, 37630, 3544, 54753,
    52242, 54748, 52240, 19282, 19284, 47806, 14262, 18692, 19231, 41254,
]

TACTICAL_LEDGER_IDS = [
    98137, 98430, 98431, 85558, 13954, 41083, 13974, 14264, 60472, 97651,
    18867, 13953, 14114, 19111, 18869, 33974, 13984, 47875, 13982, 96145,
    16056, 17498, 13815, 14127, 57119, 18989, 47700, 15764, 97956, 85469,
    41059, 13970, 54399, 40661, 14102, 33973, 14142, 17546, 94063, 14172,
    3218, 28381, 18879, 24283, 32262, 37293, 13965, 33446, 14220, 28780,
    19149, 52915, 14100, 47839, 52997, 19193, 18937, 3532, 15813, 48430,
    18963, 13945, 33513, 22108, 20157, 49729, 57120, 13998, 13920, 4294,
    35878, 47447, 22111, 13924, 28800, 28750, 22107, 25530, 47755, 88147,
    54393, 17487, 53857, 85750, 97640, 18973, 13926, 33453, 41491, 14104,
    15751, 2998, 27186, 19009, 22110, 42205, 91849, 49712, 20159, 24639,
    19200,
]

EVERGREEN_LEDGER_IDS = [
    11987, 18979, 14070, 97957, 28811, 92541, 22119, 28801, 13969, 33985,
    37452, 19187, 95632, 16064, 34590, 21889, 17918, 19335, 16057, 47891,
    15739, 37480, 19038, 40361, 41525, 28377, 19281, 78367, 47297, 17932,
    14206, 18849, 57207, 73794, 56139,
]


def _sanitize_orders(path: Path) -> dict:
    if not path.exists():
        return {"buys": {}, "sells": {}, "count": 0, "total_escrow": 0.0}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {
        "buys": {},
        "sells": {},
        "count": raw.get("count", 0),
        "total_escrow": raw.get("total_escrow", 0.0),
    }
    for side in ("buys", "sells"):
        for tid, entry in (raw.get(side) or {}).items():
            if isinstance(entry, dict):
                out[side][str(tid)] = {
                    k: v
                    for k, v in entry.items()
                    if k in ("price", "volume_remain", "volume_total", "escrow", "is_buy_order", "duration")
                }
            else:
                out[side][str(tid)] = entry
    return out


def _sanitize_assets(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    cleaned = []
    for row in raw:
        if isinstance(row, dict) and row.get("type_id") is not None:
            cleaned.append({
                "type_id": int(row["type_id"]),
                "quantity": int(row.get("quantity", 1)),
            })
    return cleaned


def _copy_json_if_exists(src: Path, dst: Path, default) -> None:
    if src.exists():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        dst.write_text(json.dumps(default, indent=2), encoding="utf-8")


def _collect_fixture_type_ids(
    src_sq: sqlite3.Connection,
    blacklist_path: Path,
    extra_random_count: int = 120,
    excluded_sample_count: int = 30,
) -> list[int]:
    active_ids = list(dict.fromkeys(HOLD_LEDGER_IDS + TACTICAL_LEDGER_IDS + EVERGREEN_LEDGER_IDS))
    id_set = set(active_ids)

    # 1. Include items matching temporary_blacklist.json (exact names, groups, or regex patterns)
    if blacklist_path.exists():
        bl_raw = json.loads(blacklist_path.read_text(encoding="utf-8"))
        if isinstance(bl_raw, dict):
            exact_names = set()
            exact_groups = set()
            patterns = []
            for key, meta in bl_raw.items():
                if isinstance(meta, dict):
                    if meta.get("active") is False:
                        continue
                    etype = str(meta.get("type", "")).lower()
                    if etype == "group":
                        exact_groups.add(str(key))
                    elif etype == "pattern":
                        patterns.append(str(key))
                    else:
                        exact_names.add(str(key))
                else:
                    exact_names.add(str(key))

            all_types = src_sq.execute(
                """
                SELECT t.typeID, t.typeName, g.groupName
                FROM invTypes t
                LEFT JOIN invGroups g ON t.groupID = g.groupID
                WHERE t.marketGroupID IS NOT NULL
                """
            ).fetchall()
            compiled = []
            for pat in patterns:
                try:
                    compiled.append(re.compile(pat, flags=re.IGNORECASE))
                except re.error:
                    pass
            for tid, tname, gname in all_types:
                if tid is None:
                    continue
                n_str = str(tname or "")
                g_str = str(gname or "")
                if n_str in exact_names or g_str in exact_groups or any(rx.search(n_str) for rx in compiled):
                    id_set.add(int(tid))

    # 2. Include sample of items in excluded categories or name patterns so exclusion logic runs
    excl_rows = src_sq.execute(
        f"""
        SELECT t.typeID
        FROM invTypes t
        JOIN invGroups g ON t.groupID = g.groupID
        JOIN invCategories c ON g.categoryID = c.categoryID
        WHERE c.categoryName IN ('Personalization', 'SKINs', 'Special Edition Assets', 'Apparel')
           OR t.typeName LIKE '%Modified%'
           OR t.typeName LIKE '%Voucher%'
           OR t.typeName LIKE '%Token%'
           OR t.typeName LIKE '%Certificate%'
        LIMIT {int(excluded_sample_count)}
        """
    ).fetchall()
    for (tid,) in excl_rows:
        if tid is not None:
            id_set.add(int(tid))

    # 3. Include sample of other active market items (non-qualifying / random universe filler)
    if id_set:
        existing_csv = ",".join(str(i) for i in sorted(id_set))
        filler_rows = src_sq.execute(
            f"""
            SELECT typeID
            FROM invTypes
            WHERE published = 1
              AND marketGroupID IS NOT NULL
              AND typeID NOT IN ({existing_csv})
            ORDER BY ABS((typeID * 2654435761) % 1000003)
            LIMIT {int(extra_random_count)}
            """
        ).fetchall()
        for (tid,) in filler_rows:
            if tid is not None:
                id_set.add(int(tid))

    ordered = active_ids + sorted(tid for tid in id_set if tid not in set(active_ids))
    return ordered


def main(history_days: int = 195) -> None:
    src_sqlite = Path(gen_led.SQLITE_PATH)
    src_duck_path = Path(gen_led.DUCKDB_PATH)
    src_blacklist = Path(bl_mod.BLACKLIST_FILE)

    if FIXTURE_DIR.exists():
        shutil.rmtree(FIXTURE_DIR)
    inputs_dir = FIXTURE_DIR / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    # Copy & sanitize JSON caches/blacklist first
    _copy_json_if_exists(src_blacklist, inputs_dir / "temporary_blacklist.json", {})
    _copy_json_if_exists(Path("ledgers/market_prices_cache.json"), inputs_dir / "market_prices_cache.json", {})
    _copy_json_if_exists(Path("ledgers/performance_cache.json"), inputs_dir / "performance_cache.json", {"items": []})
    (inputs_dir / "character_orders.json").write_text(
        json.dumps(_sanitize_orders(Path("ledgers/character_orders.json")), indent=2),
        encoding="utf-8",
    )
    (inputs_dir / "character_assets.json").write_text(
        json.dumps(_sanitize_assets(Path("ledgers/character_assets.json")), indent=2),
        encoding="utf-8",
    )

    # 1. SLICE DUCKDB & SQLITE FIRST
    src_duck = duckdb.connect(str(src_duck_path), read_only=True)
    src_sq = sqlite3.connect(str(src_sqlite))
    type_ids = _collect_fixture_type_ids(src_sq, inputs_dir / "temporary_blacklist.json")
    id_csv = ",".join(str(i) for i in type_ids)

    fixture_duck = inputs_dir / "fixture_analytics.duckdb"
    max_date = src_duck.execute("SELECT MAX(date) FROM forge_market_history").fetchone()[0]
    cutoff = max_date - datetime.timedelta(days=history_days)
    hist_df = src_duck.execute(
        f"""
        SELECT date, type_id, average, highest, lowest, order_count, volume
        FROM forge_market_history
        WHERE type_id IN ({id_csv})
          AND date >= DATE '{cutoff}'
        ORDER BY date ASC, type_id ASC
        """
    ).df()
    src_duck.close()

    dst_duck = duckdb.connect(str(fixture_duck))
    dst_duck.execute("""
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
    if not hist_df.empty:
        dst_duck.execute("INSERT INTO forge_market_history SELECT * FROM hist_df")
    dst_duck.close()

    fixture_sqlite = inputs_dir / "fixture_sde.sqlite"
    dst_sq = sqlite3.connect(str(fixture_sqlite))
    dst_sq.executescript("""
        CREATE TABLE invTypes (typeID INTEGER PRIMARY KEY, groupID INTEGER, typeName TEXT, description TEXT, marketGroupID INTEGER, published INTEGER);
        CREATE TABLE invGroups (groupID INTEGER PRIMARY KEY, categoryID INTEGER, groupName TEXT);
        CREATE TABLE invCategories (categoryID INTEGER PRIMARY KEY, categoryName TEXT);
        CREATE TABLE industryActivityProducts (typeID INTEGER, activityID INTEGER, productTypeID INTEGER, quantity INTEGER);
        CREATE TABLE invMetaTypes (typeID INTEGER, parentTypeID INTEGER, metaGroupID INTEGER);
    """)

    types_rows = src_sq.execute(
        f"SELECT typeID, groupID, typeName, description, marketGroupID, published FROM invTypes WHERE typeID IN ({id_csv})"
    ).fetchall()
    group_ids = sorted({r[1] for r in types_rows if r[1] is not None})
    grp_csv = ",".join(str(g) for g in group_ids) or "0"
    groups_rows = src_sq.execute(
        f"SELECT groupID, categoryID, groupName FROM invGroups WHERE groupID IN ({grp_csv})"
    ).fetchall()
    cat_ids = sorted({r[1] for r in groups_rows if r[1] is not None})
    cat_csv = ",".join(str(c) for c in cat_ids) or "0"
    cats_rows = src_sq.execute(
        f"SELECT categoryID, categoryName FROM invCategories WHERE categoryID IN ({cat_csv})"
    ).fetchall()
    mfg_rows = src_sq.execute(
        f"SELECT typeID, activityID, productTypeID, quantity FROM industryActivityProducts WHERE productTypeID IN ({id_csv}) AND activityID IN (1, 11)"
    ).fetchall()
    meta_rows = src_sq.execute(
        f"SELECT typeID, parentTypeID, metaGroupID FROM invMetaTypes WHERE typeID IN ({id_csv})"
    ).fetchall()
    src_sq.close()

    dst_sq.executemany("INSERT INTO invTypes VALUES (?, ?, ?, ?, ?, ?)", types_rows)
    dst_sq.executemany("INSERT INTO invGroups VALUES (?, ?, ?)", groups_rows)
    dst_sq.executemany("INSERT INTO invCategories VALUES (?, ?)", cats_rows)
    dst_sq.executemany("INSERT INTO industryActivityProducts VALUES (?, ?, ?, ?)", mfg_rows)
    dst_sq.executemany("INSERT INTO invMetaTypes VALUES (?, ?, ?)", meta_rows)
    dst_sq.commit()
    dst_sq.close()

    # 2. SWITCH PATHS, CONNECTIONS & JSON LOADERS TO FIXTURE INPUTS, THEN CALL load_market_universe()
    @contextmanager
    def _fixture_open_analytics_connection(read_only: bool = False, attach_sde: bool = True):
        os.makedirs(inputs_dir, exist_ok=True)
        con = duckdb.connect(str(fixture_duck), read_only=read_only)
        try:
            if attach_sde and fixture_sqlite.exists():
                con.execute(f"ATTACH '{fixture_sqlite}' AS sde (TYPE SQLITE);")
            yield con
        finally:
            con.close()

    gen_led.SQLITE_PATH = str(fixture_sqlite)
    gen_led.DUCKDB_PATH = str(fixture_duck)
    sde_mod.SQLITE_PATH = str(fixture_sqlite)
    sde_mod.DUCKDB_PATH = str(fixture_duck)
    analytics_db.DUCKDB_PATH = str(fixture_duck)
    analytics_db.SQLITE_PATH = str(fixture_sqlite)
    bl_mod.BLACKLIST_FILE = str(inputs_dir / "temporary_blacklist.json")

    telemetry = json.loads((inputs_dir / "market_prices_cache.json").read_text(encoding="utf-8"))
    orders_data = json.loads((inputs_dir / "character_orders.json").read_text(encoding="utf-8"))
    assets_data = json.loads((inputs_dir / "character_assets.json").read_text(encoding="utf-8"))
    perf_data = json.loads((inputs_dir / "performance_cache.json").read_text(encoding="utf-8"))
    bl_tuple = bl_mod.load_active_blacklist()

    orig_analytics_conn = analytics_db.open_analytics_connection
    orig_gen_conn = getattr(gen_led, "open_analytics_connection", None)
    orig_read_json = gen_led.read_json
    orig_load_bl = gen_led.load_active_blacklist
    orig_get_ver_out_dir = strat_mod.get_version_output_dir

    def _fixture_read_json(path, default=None):
        p = str(path)
        if p.endswith("character_orders.json"):
            return orders_data
        if p.endswith("character_assets.json"):
            return assets_data
        if p.endswith("performance_cache.json"):
            return perf_data
        if p.endswith("market_prices_cache.json"):
            return telemetry
        return orig_read_json(path, default=default)

    analytics_db.open_analytics_connection = _fixture_open_analytics_connection
    if orig_gen_conn is not None:
        gen_led.open_analytics_connection = _fixture_open_analytics_connection
    gen_led.read_json = _fixture_read_json
    gen_led.load_active_blacklist = lambda: bl_tuple

    expected_root = FIXTURE_DIR / "expected"
    expected_root.mkdir(parents=True, exist_ok=True)

    try:
        # Called AFTER slicing and wiring fixture DBs
        universe_df = gen_led.load_market_universe()

        for ver in ("v1", "v2"):
            ver_out = expected_root / ver
            ver_out.mkdir(parents=True, exist_ok=True)
            gen_led.OUTPUT_DIR = str(ver_out)
            strat_mod.get_version_output_dir = lambda v, base_dir="ledgers", out=str(ver_out): out
            gen_led.run_waterfall(
                df=universe_df,
                total_budget=GOLDEN_BUDGET,
                market_telemetry=telemetry,
                target_desks={"hold", "tactical", "evergreen"},
                strategy_version=ver,
                snapshot_time=FIXED_SNAPSHOT,
            )
    finally:
        analytics_db.open_analytics_connection = orig_analytics_conn
        if orig_gen_conn is not None:
            gen_led.open_analytics_connection = orig_gen_conn
        gen_led.read_json = orig_read_json
        gen_led.load_active_blacklist = orig_load_bl
        strat_mod.get_version_output_dir = orig_get_ver_out_dir

    manifest = {
        "snapshot_time": FIXED_SNAPSHOT.isoformat(),
        "budget": GOLDEN_BUDGET,
        "active_ledger_item_count": len(set(HOLD_LEDGER_IDS + TACTICAL_LEDGER_IDS + EVERGREEN_LEDGER_IDS)),
        "total_fixture_type_ids": len(type_ids),
        "versions": ["v1", "v2"],
        "desks": ["hold", "tactical", "evergreen"],
    }
    (FIXTURE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[+] Golden snapshot written to {FIXTURE_DIR} ({len(type_ids)} total type_ids)")


if __name__ == "__main__":
    main()