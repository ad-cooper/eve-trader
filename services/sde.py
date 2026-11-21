import os
import re
from contextlib import contextmanager
import duckdb

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"

# Packaged ship volumes (m3) by SDE groupID
PACKAGED_SHIP_M3: dict[int, float] = {
    25: 2500.0, 324: 2500.0, 830: 2500.0, 831: 2500.0, 834: 2500.0, 893: 2500.0, 1283: 2500.0, 1527: 2500.0,  # Frigates
    420: 5000.0, 541: 5000.0, 1305: 5000.0, 1534: 5000.0,  # Destroyers
    26: 10000.0, 358: 10000.0, 832: 10000.0, 833: 10000.0, 894: 10000.0, 906: 10000.0, 963: 10000.0, 1972: 10000.0,  # Cruisers
    419: 15000.0, 540: 15000.0, 1201: 15000.0,  # Battlecruisers
    27: 50000.0, 898: 50000.0, 900: 50000.0,  # Battleships
    28: 20000.0, 380: 20000.0, 1202: 20000.0,  # Haulers / DST / BR
    463: 3750.0, 543: 3750.0,  # Mining Barges / Exhumers
    941: 500000.0,  # Industrial Command Ships (Orca / Porpoise)
    513: 1000000.0, 902: 1000000.0,  # Freighters / Jump Freighters
}


def get_packaged_m3(group_id: int | None, raw_volume: float | None) -> float:
    """Returns packaged ship m3 when applicable, otherwise raw item volume (min 0.01 m3)."""
    gid = int(group_id) if group_id is not None else 0
    m3 = PACKAGED_SHIP_M3.get(gid, float(raw_volume or 0.01))
    return max(m3, 0.01)


@contextmanager
def connect_analytics(read_only: bool = True, attach_sde: bool = True):
    """Context-managed DuckDB connection with optional read-only SQLite SDE attachment."""
    if not read_only and os.path.dirname(DUCKDB_PATH):
        os.makedirs(os.path.dirname(DUCKDB_PATH), exist_ok=True)
    con = duckdb.connect(DUCKDB_PATH, read_only=read_only)
    try:
        if attach_sde and os.path.exists(SQLITE_PATH):
            con.execute(f"ATTACH '{SQLITE_PATH}' AS sde (TYPE SQLITE);")
        yield con
    finally:
        con.close()


def resolve_type_ids(type_ids: list[int]) -> dict[int, str]:
    """Maps integer SDE typeID -> typeName."""
    clean_ids = sorted({int(t) for t in (type_ids or []) if str(t).isdigit()})
    if not clean_ids:
        return {}
    try:
        with connect_analytics(read_only=True, attach_sde=True) as con:
            id_str = ",".join(str(t) for t in clean_ids)
            rows = con.execute(f"SELECT typeID, typeName FROM sde.invTypes WHERE typeID IN ({id_str})").fetchall()
            return {int(r[0]): str(r[1]) for r in rows}
    except Exception:
        return {}


def resolve_type_names(names: list[str], case_insensitive: bool = False) -> dict[str, int]:
    """Maps SDE typeName -> integer typeID."""
    clean_names = sorted({str(n).strip() for n in (names or []) if str(n).strip()})
    if not clean_names:
        return {}
    try:
        with connect_analytics(read_only=True, attach_sde=True) as con:
            if case_insensitive:
                quoted = ",".join("'" + n.lower().replace("'", "''") + "'" for n in clean_names)
                rows = con.execute(
                    f"SELECT typeID, typeName FROM sde.invTypes WHERE LOWER(typeName) IN ({quoted})"
                ).fetchall()
                by_lower = {str(r[1]).lower(): int(r[0]) for r in rows}
                return {n: by_lower[n.lower()] for n in clean_names if n.lower() in by_lower}
            else:
                quoted = ",".join("'" + n.replace("'", "''") + "'" for n in clean_names)
                rows = con.execute(
                    f"SELECT typeID, typeName FROM sde.invTypes WHERE typeName IN ({quoted})"
                ).fetchall()
                return {str(r[1]): int(r[0]) for r in rows}
    except Exception:
        return {}


def resolve_sde_items(
    names: list[str] | None = None,
    type_ids: list[int] | None = None,
) -> tuple[dict[int, dict], dict[str, dict]]:
    """
    Bulk-resolves SDE metadata (type_id, name, unit_m3, group_id, group_name) by name and/or type_id.
    Returns (by_type_id, by_lower_name).
    """
    clean_names = sorted({str(n).strip() for n in (names or []) if str(n).strip()})
    clean_tids = sorted({int(t) for t in (type_ids or []) if t})
    if not clean_names and not clean_tids:
        return {}, {}

    by_tid: dict[int, dict] = {}
    by_name: dict[str, dict] = {}
    try:
        with connect_analytics(read_only=True, attach_sde=True) as con:
            clauses = []
            if clean_names:
                in_names = ",".join("'" + n.lower().replace("'", "''") + "'" for n in clean_names)
                clauses.append(f"LOWER(t.typeName) IN ({in_names})")
            if clean_tids:
                in_tids = ",".join(str(t) for t in clean_tids)
                clauses.append(f"t.typeID IN ({in_tids})")

            rows = con.execute(f"""
                SELECT t.typeID, t.typeName, t.volume, t.groupID, COALESCE(g.groupName, '')
                FROM sde.invTypes t
                LEFT JOIN sde.invGroups g ON t.groupID = g.groupID
                WHERE ({' OR '.join(clauses)})
            """).fetchall()

            for tid, tname, raw_vol, gid, gname in rows:
                rec = {
                    "type_id": int(tid),
                    "name": str(tname),
                    "unit_m3": get_packaged_m3(gid, raw_vol),
                    "group_id": int(gid) if gid is not None else 0,
                    "group_name": str(gname),
                }
                by_tid[int(tid)] = rec
                by_name[str(tname).lower()] = rec
    except Exception:
        pass

    return by_tid, by_name


def get_latest_ledger_targets(type_ids: list[int]) -> dict[int, dict]:
    """
    Fetches the most recent `ledger_history` snapshot per type_id from DuckDB.
    Returns {type_id: {'name': str, 'desk': str, 'target_buy': float, 'target_sell': float}}.
    """
    clean_ids = sorted({int(t) for t in (type_ids or []) if str(t).isdigit()})
    if not clean_ids:
        return {}
    try:
        with connect_analytics(read_only=True, attach_sde=False) as con:
            has_table = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'ledger_history'"
            ).fetchone()[0]
            if has_table == 0:
                return {}
            id_str = ",".join(str(t) for t in clean_ids)
            rows = con.execute(f"""
                WITH RankedHistory AS (
                    SELECT
                        type_id,
                        name,
                        desk_type,
                        target_buy,
                        target_sell,
                        ROW_NUMBER() OVER(PARTITION BY type_id ORDER BY snapshot_time DESC) AS rn
                    FROM ledger_history
                    WHERE type_id IN ({id_str})
                )
                SELECT type_id, name, desk_type, target_buy, target_sell
                FROM RankedHistory
                WHERE rn = 1;
            """).fetchall()
            return {
                int(r[0]): {
                    "name": str(r[1]),
                    "desk": str(r[2]).upper(),
                    "target_buy": float(r[3] or 0.0),
                    "target_sell": float(r[4] or 0.0),
                }
                for r in rows
            }
    except Exception:
        return {}


def parse_eve_item_lines(raw_text: str) -> list[dict]:
    """
    Parses pasted EVE Inventory (tab-separated 'Name\tQty\t...') or MultiBuy ('Name 5' / 'Name x5')
    into [{'name': str, 'qty': int}]. Skips table headers and aggregates duplicate lines.
    """
    if not raw_text:
        return []
    counts: dict[str, int] = {}
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("item\t") or low.startswith("name\t") or low in ("item", "name", "item name"):
            continue

        name = None
        qty = 1
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
            name = parts[0]
            if len(parts) >= 2 and parts[1]:
                clean_q = re.sub(r"[^\d]", "", parts[1])
                if clean_q:
                    qty = max(1, int(clean_q))
        else:
            m = re.match(r"^(.*?)(?:,|\s)+(?:x\s*)?(\d[\d,]*)\s*$", line, flags=re.IGNORECASE)
            if m and m.group(1).strip():
                name = m.group(1).strip()
                qty = max(1, int(m.group(2).replace(",", "")))
            else:
                name = line
                qty = 1

        if name:
            counts[name] = counts.get(name, 0) + qty

    return [{"name": k, "qty": v} for k, v in counts.items()]
