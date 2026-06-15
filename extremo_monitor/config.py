"""Centralised configuration for Extremo Monitor."""

from dataclasses import dataclass, field
from typing import List


@dataclass
class ExtremeThresholds:
    """Thresholds that define what constitutes an *extreme* for each signal."""

    # Signal 1 – Drawdown from ATH
    drawdown_min: float = 0.75  # ≥75 % drawdown from all-time high

    # Signal 2 – Weekly RSI
    rsi_period: int = 14
    rsi_max: float = 25.0  # RSI(14) weekly ≤ 25

    # Signal 3 – Distance to MA200 daily (in standard deviations)
    ma200_sigma_min: float = 2.0  # ≥ 2σ below MA200

    # Signal 4 – Relative volume (capitulation spike)
    volume_ma_period: int = 20
    volume_multiplier: float = 3.0  # current vol ≥ 3× MA20(vol)

    # Signal 5 – Historical percentile
    percentile_max: float = 5.0  # price in bottom 5 % of all-time range

    # Confluence
    min_confluence: int = 3  # at least 3 of 5 signals must fire


@dataclass
class SurvivalFilter:
    """Criteria to exclude assets unlikely to recover."""

    min_volume_24h_usd: float = 1_000_000.0  # minimum daily volume in USD
    min_listing_days: int = 180  # listed for at least 6 months
    excluded_symbols: List[str] = field(
        default_factory=lambda: [
            # Stablecoins – strategy does not apply
            "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "EURI", "USDD",
            # Wrapped / pegged
            "WBTC", "WETH", "STETH",
        ]
    )


@dataclass
class PositionConfig:
    """How positions are sized and managed."""

    max_positions: int = 50
    # Ladder distribution (must sum to 1.0)
    ladder_pcts: List[float] = field(
        default_factory=lambda: [0.40, 0.30, 0.20, 0.10]
    )
    # Ladder price discounts from entry (cumulative)
    ladder_discounts: List[float] = field(
        default_factory=lambda: [0.00, 0.15, 0.30, 0.45]
    )
    # Take-profit levels: (fraction_to_close, target_multiplier)
    take_profits: List[tuple] = field(
        default_factory=lambda: [
            (0.25, 1.50),  # close 25 % at +50 %
            (0.25, 2.00),  # close 25 % at +100 %
            (0.25, 3.00),  # close 25 % at +200 %
            (0.25, None),  # trailing stop for remaining 25 %
        ]
    )
    trailing_stop_pct: float = 0.20  # 20 % trailing for last tranche


@dataclass
class CollateralConfig:
    """Constraints for the optional collateralisation layer."""

    target_ltv: float = 0.10  # 10 % loan-to-value
    max_apr: float = 0.005  # only collateralise if APR ≤ 0.5 %
    min_margin_to_liquidation: float = 0.80  # ≥80 % price drop buffer


@dataclass
class MonitorConfig:
    """Top-level configuration combining all sub-configs."""

    thresholds: ExtremeThresholds = field(default_factory=ExtremeThresholds)
    survival: SurvivalFilter = field(default_factory=SurvivalFilter)
    position: PositionConfig = field(default_factory=PositionConfig)
    collateral: CollateralConfig = field(default_factory=CollateralConfig)

    # Capital
    total_capital: float = 10_000.0  # USD budget for the strategy

    # Scan settings
    scan_interval_hours: int = 4  # how often the scanner runs
    quote_asset: str = "USDT"  # only scan *USDT pairs

    # Data
    klines_db_path: str = "klines.db"
    monitor_db_path: str = "extremo_monitor.db"
