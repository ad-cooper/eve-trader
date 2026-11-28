import pandas as pd

from services.analytics_db import query_analytics_df
from services.desk_config import UNIVERSE_QUERY_CONFIG, UniverseQueryConfig
from services.exclusions import (
    sql_excluded_market_groups_clause,
    sql_non_expired_description_clause,
)
from services.ledgers import (
    IncumbentState
)
from services.desk_config import DESK_PROFILES, UNIVERSE_QUERY_CONFIG, UniverseQueryConfig
from services.exclusions import (
    build_static_exclusion_mask,
    sql_excluded_market_groups_clause,
    sql_non_expired_description_clause,
)

def load_market_universe(qcfg: UniverseQueryConfig = UNIVERSE_QUERY_CONFIG) -> pd.DataFrame:
    """Loads the candidate market universe DataFrame from analytics + attached SDE."""
    return query_analytics_df(build_market_universe_query(qcfg), read_only=True, attach_sde=True)

def build_market_universe_query(qcfg: UniverseQueryConfig = UNIVERSE_QUERY_CONFIG) -> str:
    """Builds the SQL query used to compute rolling market metrics and join SDE metadata."""
    bid_share_expr = (
        f"CASE WHEN highest > lowest "
        f"THEN LEAST({qcfg.bid_share_max}, GREATEST({qcfg.bid_share_min}, "
        f"(highest - LEAST(GREATEST(average, lowest), highest)) / (highest - lowest))) "
        f"ELSE {qcfg.bid_share_fallback} END"
    )
    dump_cond_expr = (
        f"(average - lowest) / NULLIF(average, 0) >= {qcfg.dump_drop_threshold} "
        f"AND ("
        f"CASE WHEN highest > lowest "
        f"THEN (highest - LEAST(GREATEST(average, lowest), highest)) / (highest - lowest) "
        f"ELSE {qcfg.bid_share_fallback} END"
        f") >= {qcfg.dump_min_bid_share}"
    )

    return f"""
    WITH metrics_180d AS (
        SELECT
            type_id,
            COUNT(*) AS total_history_days,
            MAX(average) AS high_180d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_180d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL {qcfg.history_lookback_days} DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_30d AS (
        SELECT
            COUNT(*) AS active_days_30d,
            type_id,
            AVG(volume) AS daily_vol_30d,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_30d,
            AVG((highest - lowest) / NULLIF(average, 0)) * 100.0 AS avg_spread_30d,
            SUM(volume * ({bid_share_expr})) / NULLIF(SUM(volume), 0) AS bid_share_30d,
            (COUNT(CASE WHEN {dump_cond_expr} THEN 1 END) * 100.0 / COUNT(*)) AS dump_freq_pct
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL {qcfg.medium_lookback_days} DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_7d AS (
        SELECT
            type_id,
            SUM(average * volume) / NULLIF(SUM(volume), 0) AS vwap_7d,
            MIN(lowest) AS min_low_7d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL {qcfg.short_lookback_days} DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    ),
    metrics_floors AS (
        SELECT
            type_id,
            QUANTILE_CONT(CASE WHEN date >= (SELECT MAX(date) - INTERVAL {qcfg.floor_lookback_days} DAY FROM forge_market_history) THEN lowest END, {qcfg.min_low_quantile}) AS min_low_14d,
            QUANTILE_CONT(lowest, {qcfg.p20_low_quantile}) AS p20_low_30d
        FROM forge_market_history
        WHERE date >= (SELECT MAX(date) - INTERVAL {qcfg.medium_lookback_days} DAY FROM forge_market_history) AND volume > 0
        GROUP BY type_id
    )
    SELECT
        m.type_id,
        t.typeName AS item_name,
        g.groupName AS group_name,
        c.categoryName AS category_name,
        l.total_history_days,
        m.active_days_30d,
        l.vwap_180d / 1e6 AS price_m_180d,
        m.vwap_30d / 1e6 AS price_m_30d,
        w.vwap_7d / 1e6 AS price_m_7d,
        f.min_low_14d / 1e6 AS min_low_m_14d,
        f.p20_low_30d / 1e6 AS p20_low_m_30d,
        m.daily_vol_30d,
        COALESCE(m.bid_share_30d, {qcfg.default_bid_share_30d}) AS bid_share_30d,
        m.dump_freq_pct,
        m.avg_spread_30d,
        (m.daily_vol_30d * (m.vwap_30d / 1e6)) AS daily_turnover_m
    FROM metrics_30d m
    JOIN metrics_7d w ON m.type_id = w.type_id
    JOIN metrics_floors f ON m.type_id = f.type_id
    JOIN metrics_180d l ON m.type_id = l.type_id
    JOIN sde.invTypes t ON m.type_id = t.typeID
    JOIN sde.invGroups g ON t.groupID = g.groupID
    JOIN sde.invCategories c ON g.categoryID = c.categoryID
    WHERE t.published = 1
      AND t.marketGroupID IS NOT NULL
      AND {sql_excluded_market_groups_clause("t.marketGroupID")}
      AND {sql_non_expired_description_clause("t.description")}
    """

def build_universe_exclusion_mask(
    df: pd.DataFrame,
    claimed_ids: set[int],
    incumbents: IncumbentState,
    bl_items: set[str] | None = None,
    bl_groups: set[str] | None = None,
    bl_meta: dict | None = None,
    losing_tids: set[int] | None = None,
    extra_excluded_categories: tuple[str, ...] = (),
) -> pd.Series:
    """Builds the boolean eligibility mask separating permanent/seasonal exclusions from desk screening."""
    keep = build_static_exclusion_mask(
        df=df,
        bl_items=bl_items,
        bl_groups=bl_groups,
        bl_meta=bl_meta,
        extra_excluded_categories=extra_excluded_categories,
    )

    sticky_ids = set(incumbents.desk_sticky_ids or ()) if incumbents else set()
    tids = df["type_id"] if "type_id" in df.columns else None

    if claimed_ids and tids is not None:
        keep &= ~tids.isin(claimed_ids)

    if losing_tids and tids is not None:
        protected = (
            set((incumbents.active_sells or {}).keys())
            | set(incumbents.significant_assets or ())
            | sticky_ids
        )
        blocked_losers = set(losing_tids) - protected
        if blocked_losers:
            keep &= ~tids.isin(blocked_losers)

    if "price_m_7d" in df.columns and "price_m_30d" in df.columns:
        dip_cfg = DESK_PROFILES["evergreen"].selection
        valid_30d = df["price_m_30d"] > 0
        valid_7d = df["price_m_7d"] > 0
        ratio = df["price_m_7d"] / df["price_m_30d"].replace(0, float("nan"))
        dip_mask = (valid_30d & valid_7d & (ratio < dip_cfg.dip_ratio_7d_30d)) | (
            valid_30d & (~valid_7d | (ratio < dip_cfg.sticky_dip_ratio_7d_30d))
        )
        if tids is not None and sticky_ids:
            dip_mask &= ~tids.isin(sticky_ids)
        keep &= ~dip_mask

    return keep
