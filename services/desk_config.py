from __future__ import annotations

from dataclasses import dataclass, field

BUDGET_FILL_STOP_RATIO: float = 0.995

@dataclass(frozen=True)
class LiveOrderPricing:
    tick_increment: float = 0.01
    spot_sell_weight: float = 0.50
    csv_sell_weight: float = 0.50
    csv_sell_cap_mult: float | None = None
    buy_ceil_ratio: float | None = None
    price_sync_tolerance: float = 0.01
    hold_prime_prox_min: float = 60.0
    hold_prime_prox_max: float = 92.0
    hold_prime_min_margin: float = 20.0
    hold_low_vol_threshold: float = 2.5
    hold_low_vol_max_capital: float = 500_000_000.0
    hold_prime_dip_limit: int = 8

@dataclass(frozen=True)
class DeskPricingConfig:
    fallback_low_ratio: float
    fallback_bid_ratio: float
    tick_increment: float
    stability_min: float
    stability_max: float
    stability_drift_weight: float
    stability_dump_weight: float
    sell_ask_cap_mult: float
    buy_cap_ratio: float
    stabilized_ask_base_weight: float = 0.95
    stabilized_ask_score_weight: float = 0.05
    sell_stabilized_weight: float = 0.50
    sell_baseline_weight: float = 0.50
    sell_vwap30_mult: float | None = None
    sell_vwap180_mult: float | None = None
    sell_vwap30_cap_mult: float | None = None
    max_live_ask_vwap180_ratio: float | None = None

@dataclass(frozen=True)
class DeskSelectionConfig:
    min_bid_share_30d: float = 0.12
    edge_baseline_pct: float = 5.35
    max_effective_edge_pct: float = 45.0
    swing_amp_min_pct: float = 14.0
    turnover_archetype_floor_m: float = 120.0
    min_dump_freq_floor_pct: float = 22.0
    dump_freq_floor_offset_pct: float = 5.0
    non_archetype_min_dump_freq_pct: float = 45.0
    tactical_min_daily_vol: float = 1.5
    hold_min_daily_vol: float = 1.0
    hold_min_active_days: int = 12
    hold_deep_mfg_ratio_cap: float = 0.85
    hold_deep_mfg_ratio_offset: float = 0.05
    hold_target_vwap180_mult: float = 0.96
    momentum_guard_bounds: tuple[float, float] = (0.75, 1.05)
    profit_velocity_elasticity_weight: float = 0.3
    ask_cap_vwap30_mult: float = 1.40
    thin_lowball_max_days_supply: float = 0.35
    thin_lowball_min_spread_gap: float = 0.03
    dip_ratio_7d_30d: float = 0.78
    sticky_dip_ratio_7d_30d: float = 0.68
    default_bid_share_30d: float = 0.35
    fallback_bid_share_30d: float = 1.0
    log_turnover_weight_base: float = 1.0

@dataclass(frozen=True)
class DeskProfile:
    name: str
    ratio: float
    vol_pct: float
    max_line_cap: float
    min_line_cap: float
    min_margin: float
    live_min_margin: float
    sticky_margin_floor: float
    cancel_floor: float
    max_hold_days: int
    take_profit_days: int
    take_profit_roi: float
    sort_col: str
    price_col: str
    min_price_m: float
    max_price_m: float
    min_turnover_m: float
    min_spread_pct: float
    min_dump_freq_pct: float = 0.0
    min_daily_vol: float = 0.0
    min_history_days: int = 0
    max_vwap_ratio_180d: float | None = None
    excluded_categories: tuple[str, ...] = ()
    sticky_min_cap_ratio: float = 0.35
    bid_share_bounds: tuple[float, float] = (0.05, 0.95)
    pricing: DeskPricingConfig | None = None
    selection: DeskSelectionConfig = field(default_factory=DeskSelectionConfig)
    live_pricing: LiveOrderPricing = field(default_factory=LiveOrderPricing)

DESK_PROFILES: dict[str, DeskProfile] = {
    "hold": DeskProfile(
        name="hold",
        ratio=0.20,
        vol_pct=0.06,
        max_line_cap=1.5e9,
        min_line_cap=150.0e6,
        min_margin=20.0,
        live_min_margin=20.0,
        sticky_margin_floor=10.0,
        cancel_floor=10.0,
        max_hold_days=21,
        take_profit_days=4,
        take_profit_roi=8.0,
        sort_col="upside_pct",
        price_col="price_m_180d",
        min_price_m=15.0,
        max_price_m=2500.0,
        min_turnover_m=10.0,
        min_spread_pct=8.0,
        min_history_days=60,
        max_vwap_ratio_180d=0.90,
        excluded_categories=("Skill",),
        pricing=DeskPricingConfig(
            fallback_low_ratio=0.94,
            fallback_bid_ratio=0.92,
            tick_increment=0.01,
            stability_min=0.70,
            stability_max=1.0,
            stability_drift_weight=1.2,
            stability_dump_weight=0.2,
            sell_ask_cap_mult=1.25,
            buy_cap_ratio=0.94,
            sell_vwap30_mult=1.220,
            sell_vwap180_mult=0.960,
            max_live_ask_vwap180_ratio=0.90,
        ),
    ),
    "tactical": DeskProfile(
        name="tactical",
        ratio=0.45,
        vol_pct=0.15,
        max_line_cap=1.5e9,
        min_line_cap=100.0e6,
        min_margin=9.0,
        live_min_margin=9.0,
        sticky_margin_floor=4.5,
        cancel_floor=4.5,
        max_hold_days=10,
        take_profit_days=3,
        take_profit_roi=4.5,
        sort_col="swing_score",
        price_col="price_m_30d",
        min_price_m=3.0,
        max_price_m=2500.0,
        min_turnover_m=25.0,
        min_spread_pct=13.5,
        min_dump_freq_pct=35.0,
        pricing=DeskPricingConfig(
            fallback_low_ratio=0.94,
            fallback_bid_ratio=0.92,
            tick_increment=0.01,
            stability_min=0.70,
            stability_max=1.0,
            stability_drift_weight=1.2,
            stability_dump_weight=0.2,
            sell_ask_cap_mult=1.25,
            buy_cap_ratio=0.95,
            sell_vwap30_mult=1.045,
        ),
    ),
    "evergreen": DeskProfile(
        name="evergreen",
        ratio=0.35,
        vol_pct=0.22,
        max_line_cap=1.2e9,
        min_line_cap=100.0e6,
        min_margin=7.0,
        live_min_margin=7.0,
        sticky_margin_floor=2.0,
        cancel_floor=2.0,
        max_hold_days=3,
        take_profit_days=0,
        take_profit_roi=2.0,
        sort_col="profit_velocity",
        price_col="price_m_30d",
        min_price_m=1.0,
        max_price_m=2500.0,
        min_turnover_m=45.0,
        min_spread_pct=13.0,
        min_daily_vol=3.0,
        pricing=DeskPricingConfig(
            fallback_low_ratio=0.94,
            fallback_bid_ratio=0.92,
            tick_increment=0.01,
            stability_min=0.70,
            stability_max=1.0,
            stability_drift_weight=1.2,
            stability_dump_weight=0.2,
            sell_ask_cap_mult=1.25,
            buy_cap_ratio=0.98,
            sell_vwap30_mult=1.035,
            sell_vwap30_cap_mult=1.065,
            sell_stabilized_weight=0.45,
            sell_baseline_weight=0.55,
        ),
    ),
}

@dataclass(frozen=True)
class UniverseQueryConfig:
    history_lookback_days: int = 180
    medium_lookback_days: int = 30
    short_lookback_days: int = 7
    floor_lookback_days: int = 14
    min_low_quantile: float = 0.25
    p20_low_quantile: float = 0.20
    bid_share_min: float = 0.02
    bid_share_max: float = 0.95
    bid_share_fallback: float = 0.25
    dump_drop_threshold: float = 0.045
    dump_min_bid_share: float = 0.25
    default_bid_share_30d: float = 0.35

UNIVERSE_QUERY_CONFIG = UniverseQueryConfig()