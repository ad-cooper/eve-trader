import datetime
import duckdb
import pandas as pd

from contextlib import contextmanager
from constants import DUCKDB_PATH, SQLITE_PATH

@contextmanager
def open_analytics_connection(
    read_only: bool = True,
    attach_sde: bool = True,
    db_path: str = DUCKDB_PATH,
    sde_path: str = SQLITE_PATH,
):
    con = duckdb.connect(str(db_path), read_only=read_only)
    try:
        if attach_sde and sde_path:
            con.execute(f"ATTACH '{sde_path}' AS sde (TYPE SQLITE);")
        yield con
    finally:
        con.close()


def query_analytics_df(
    sql: str,
    params: list | tuple | None = None,
    read_only: bool = True,
    attach_sde: bool = False,
) -> pd.DataFrame:
    with open_analytics_connection(read_only=read_only, attach_sde=attach_sde) as con:
        if params is not None:
            return con.execute(sql, params).df()
        return con.execute(sql).df()


def query_historical_buys_by_names(item_names: list[str], limit_per_item: int = 10) -> dict[str, dict]:
    if not item_names:
        return {}
    quoted_names = ",".join("'" + n.replace("'", "''") + "'" for n in set(item_names) if n)
    if not quoted_names:
        return {}

    sql = f"""
    WITH ranked AS (
        SELECT
            item_name,
            price,
            quantity,
            date,
            ROW_NUMBER() OVER (PARTITION BY item_name ORDER BY date DESC) AS rn
        FROM historical_wallet_buys
        WHERE item_name IN ({quoted_names})
    )
    SELECT
        item_name,
        ROUND(SUM(price * quantity) / SUM(quantity), 2) AS avg_price,
        MIN(date) AS first_date
    FROM ranked
    WHERE rn <= {int(limit_per_item)}
    GROUP BY item_name;
    """
    out: dict[str, dict] = {}
    with open_analytics_connection(read_only=True, attach_sde=False) as con:
        for iname, avg_p, first_dt in con.execute(sql).fetchall():
            out[iname] = {"avg_price": float(avg_p or 0.0), "first_date": first_dt}
    return out


def query_30d_vwap_by_type_ids(type_ids: list[int]) -> dict[int, float]:
    if not type_ids:
        return {}
    id_str = ",".join(str(int(t)) for t in set(type_ids))
    sql = f"""
        SELECT
            type_id,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap
        FROM forge_market_history
        WHERE type_id IN ({id_str})
          AND date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM forge_market_history)
        GROUP BY type_id;
    """
    out: dict[int, float] = {}
    with open_analytics_connection(read_only=True, attach_sde=False) as con:
        for tid, vwap in con.execute(sql).fetchall():
            if vwap and vwap > 0:
                out[int(tid)] = float(vwap)
    return out


def query_earliest_buy_and_snapshot_dates(
    item_names: list[str],
    type_ids: list[int],
) -> tuple[dict[str, object], dict[int, object]]:
    clean_buys: dict[str, object] = {}
    snap_dates: dict[int, object] = {}
    with open_analytics_connection(read_only=True, attach_sde=False) as con:
        if item_names:
            quoted_names = ",".join("'" + n.replace("'", "''") + "'" for n in set(item_names) if n)
            if quoted_names:
                q_clean = (
                    f"SELECT item_name, MIN(tx_date) FROM clean_transactions "
                    f"WHERE tx_type = 'buy' AND item_name IN ({quoted_names}) GROUP BY item_name"
                )
                for iname, dt_val in con.execute(q_clean).fetchall():
                    if dt_val:
                        clean_buys[iname] = dt_val

        if type_ids:
            id_str = ",".join(str(int(t)) for t in set(type_ids))
            q_snap = (
                f"SELECT type_id, MIN(snapshot_time) FROM ledger_history "
                f"WHERE type_id IN ({id_str}) GROUP BY type_id"
            )
            for tid_row, snap_dt in con.execute(q_snap).fetchall():
                if snap_dt:
                    snap_dates[int(tid_row)] = snap_dt
    return clean_buys, snap_dates

def record_ledger_history_snapshot(
    df_ledger: pd.DataFrame,
    desk_type: str,
    snapshot_time=None,
    db_path: str | None = None,
) -> None:
    """Ensures `ledger_history` schema and appends a snapshot from `df_ledger`."""
    if df_ledger.empty:
        return

    snap_ts = snapshot_time or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

    df_history = df_ledger.copy()
    if "supply_class" not in df_history.columns:
        df_history["supply_class"] = "LEGACY_V1"
    df_history.insert(0, "desk_type", desk_type)
    df_history.insert(0, "snapshot_time", snap_ts)
    df_history["strategy_version"] = "v2" # This is deprecated for now, but we are keeping the v1/v2 data

    con = duckdb.connect(db_path) if db_path else duckdb.connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ledger_history (
                snapshot_time TIMESTAMP,
                desk_type VARCHAR,
                type_id INTEGER,
                name VARCHAR,
                qty INTEGER,
                dump_freq DOUBLE,
                target_buy DOUBLE,
                target_sell DOUBLE,
                net_margin DOUBLE,
                capital DOUBLE,
                profit DOUBLE,
                strategy_version VARCHAR DEFAULT 'v1',
                supply_class VARCHAR
            );
        """)
        con.execute("ALTER TABLE ledger_history ADD COLUMN IF NOT EXISTS strategy_version VARCHAR DEFAULT 'v1'")
        con.execute("ALTER TABLE ledger_history ADD COLUMN IF NOT EXISTS supply_class VARCHAR")
        con.execute("""
            INSERT INTO ledger_history (
                snapshot_time, desk_type, type_id, name, qty, dump_freq,
                target_buy, target_sell, net_margin, capital, profit,
                strategy_version, supply_class
            )
            SELECT
                snapshot_time, desk_type, type_id, name, qty, dump_freq,
                target_buy, target_sell, net_margin, capital, profit,
                strategy_version, supply_class
            FROM df_history
        """)
    finally:
        con.close()