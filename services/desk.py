from dataclasses import dataclass, field
import os
import pandas as pd

from constants import DUCKDB_PATH, OUTPUT_DIR
from services.analytics_db import record_ledger_history_snapshot
from services.desk_config import DESK_PROFILES, DeskProfile
from services.storage import read_json


@dataclass(frozen=True)
class IncumbentState:
    active_buys: dict[int, dict]
    active_sells: dict[int, dict]
    asset_qtys: dict[int, int]
    significant_assets: set[int]
    desk_sticky_ids: set[int]


def resolve_desk_incumbents(
    df: pd.DataFrame,
    desk_type: str,
    claimed_ids: set[int],
    active_buys: dict[int, dict],
    active_sells: dict[int, dict],
    asset_qtys: dict[int, int],
    prior_desks: dict[int, str],
    min_asset_value_isk: float = 50_000_000.0,
) -> IncumbentState:
    """Identifies active orders and high-value hangar stacks that must remain sticky on this desk."""
    price_lookup = dict(zip(df["type_id"].astype(int), df["price_m_30d"] * 1e6))
    significant_assets = {
        tid for tid, qty in asset_qtys.items()
        if (qty * price_lookup.get(tid, 0.0)) >= min_asset_value_isk
    }
    incumbent_ids = set(active_buys.keys()) | set(active_sells.keys()) | significant_assets

    other_prior_desks = {tid for tid, d in prior_desks.items() if d != desk_type}
    if desk_type == "evergreen":
        desk_sticky_ids = (incumbent_ids - claimed_ids) - other_prior_desks
    else:
        desk_sticky_ids = {
            tid for tid in (incumbent_ids - claimed_ids)
            if prior_desks.get(tid) == desk_type
        }

    return IncumbentState(
        active_buys=active_buys,
        active_sells=active_sells,
        asset_qtys=asset_qtys,
        significant_assets=significant_assets,
        desk_sticky_ids=desk_sticky_ids,
    )


@dataclass
class Desk:
    desk_type: str
    strategy_version: str = "v1"
    output_dir: str = OUTPUT_DIR
    db_path: str = DUCKDB_PATH
    profile: DeskProfile = field(init=False)
    _positions_cache: tuple[dict, dict, dict, dict] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.desk_type = str(self.desk_type or "evergreen").strip().lower()
        self.profile = self.get_desk_profile(self.desk_type)

    @staticmethod
    def get_desk_profile(desk_type: str | None) -> DeskProfile:
        """Case-insensitive lookup for a DeskProfile, defaulting to 'evergreen'."""
        key = str(desk_type or "evergreen").strip().lower()
        return DESK_PROFILES.get(key, DESK_PROFILES["evergreen"])

    @property
    def ratio(self) -> float:
        return self.profile.ratio

    @property
    def ledger_dir(self) -> str:
        from services.strategy_versions import get_version_output_dir
        return get_version_output_dir(self.strategy_version, base_dir=self.output_dir)

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.ledger_dir, f"{self.desk_type}_ledger.csv")

    def max_line_cap(self, scale_factor: float = 1.0) -> float:
        return self.profile.max_line_cap * scale_factor

    def claimed_ids_from_csv(self) -> set[int]:
        if not os.path.exists(self.ledger_path):
            return set()
        df = pd.read_csv(self.ledger_path)
        return set(df["type_id"].dropna().astype(int).tolist())

    def existing_state(self) -> tuple[list[int], float]:
        if not os.path.exists(self.ledger_path):
            return [], 0.0
        df_existing = pd.read_csv(self.ledger_path)
        if df_existing.empty or "type_id" not in df_existing.columns:
            return [], 0.0
        ids = df_existing["type_id"].dropna().astype(int).tolist()
        cap = float(df_existing["capital"].fillna(0.0).sum()) if "capital" in df_existing.columns else 0.0
        return ids, cap

    @classmethod
    def load_existing_desk_state(
        cls,
        desk_type: str,
        strategy_version: str = "v1",
        base_dir: str = OUTPUT_DIR,
    ) -> tuple[list[int], float]:
        """Reads claimed type_ids and deployed capital from an existing desk CSV."""
        return cls(
            desk_type=desk_type,
            strategy_version=strategy_version,
            output_dir=base_dir,
        ).existing_state()

    def load_positions(self, refresh: bool = False) -> tuple[dict, dict, dict, dict]:
        if self._positions_cache is not None and not refresh:
            return self._positions_cache

        odata = read_json("ledgers/character_orders.json") or {}
        buys = {int(k): v for k, v in (odata.get("buys") or {}).items() if str(k).isdigit()}
        sells = {int(k): v for k, v in (odata.get("sells") or {}).items() if str(k).isdigit()}

        assets: dict[int, int] = {}
        adata = read_json("ledgers/character_assets.json")
        if isinstance(adata, list):
            for row in adata:
                tid = row.get("type_id")
                if tid is not None:
                    assets[int(tid)] = assets.get(int(tid), 0) + int(row.get("quantity", 1))

        prior_desks: dict[int, str] = {}
        for dname in DESK_PROFILES:
            prior_path = os.path.join(self.ledger_dir, f"{dname}_ledger.csv")
            if os.path.exists(prior_path):
                pdf = pd.read_csv(prior_path)
                for tid in pdf["type_id"].dropna().astype(int).tolist():
                    prior_desks[int(tid)] = dname

        self._positions_cache = (buys, sells, assets, prior_desks)
        return self._positions_cache

    def resolve_incumbents(self, df: pd.DataFrame, claimed_ids: set[int]) -> IncumbentState:
        buys, sells, assets, prior_desks = self.load_positions()
        return resolve_desk_incumbents(
            df=df,
            desk_type=self.desk_type,
            claimed_ids=claimed_ids,
            active_buys=buys,
            active_sells=sells,
            asset_qtys=assets,
            prior_desks=prior_desks,
        )

    def persist_ledger(
        self,
        ledger_rows: list[dict],
        allocated_cap: float,
        budget: float,
        sticky_kept: int,
        snapshot_time=None,
    ) -> None:
        os.makedirs(self.ledger_dir, exist_ok=True)
        df_ledger = pd.DataFrame(ledger_rows)
        df_ledger.to_csv(self.ledger_path, index=False)
        print(
            f"[+][{self.strategy_version}] Saved {len(ledger_rows)} lines to {self.ledger_path} "
            f"({sticky_kept} sticky | Allocated: {allocated_cap/1e9:.2f}B / {budget/1e9:.2f}B)"
        )
        if df_ledger.empty:
            return
        try:
            record_ledger_history_snapshot(
                df_ledger=df_ledger,
                desk_type=self.desk_type,
                strategy_version=self.strategy_version,
                snapshot_time=snapshot_time,
                db_path=self.db_path,
            )
        except Exception as e:
            print(f"[!] Failed to log {self.strategy_version} history to DuckDB: {e}")