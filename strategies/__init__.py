"""
Modular Strategies Package
==========================
Exports standardized, modular trading strategies and mathematical routines for KCEX:
- BaseStrategy: Strategy lifecycle interface
- EMACrossoverStrategy: Fast/Slow EMA crossover momentum/trend trading
- StochasticRSIStrategy: High-frequency Stochastic RSI mean-reversion & momentum scalping
"""

from strategies.base import BaseStrategy, BaseSubStrategy
from strategies.ema_crossover import (
    EMACrossoverStrategy,
    EMACrossoverSubStrategy,
    EMA_PRESETS,
    compute_ema_series
)
from strategies.stoch_rsi import (
    StochasticRSIStrategy,
    StochasticRSISubStrategy,
    STOCH_RSI_PRESETS,
    compute_rsi_series,
    compute_stoch_rsi
)

from strategies.filters import (
    BaseFilter,
    HTFTrendFilter,
    ADXRegimeFilter,
    HourlySessionFilter,
    DirectionalBiasFilter,
    FilterPipeline,
    compute_atr_series,
    compute_adx_series
)
from strategies.smart_strategy import (
    SmartStrategy,
    SmartSubStrategy,
    MarketRegime,
    compute_chop_series
)
from strategies.microstructure_alpha import (
    VPINCalculator,
    TickRunLengthDetector,
    OrderFlowImbalance
)
from strategies.volatility_regime import (
    compute_bollinger_bandwidth,
    compute_choppiness_index,
    AdaptiveRegimeSwitcher
)
try:
    from strategies.ml_strategy import (
        MLStrategy,
        MLSubStrategy
    )
except ImportError:
    MLStrategy = None
    MLSubStrategy = None
from strategies.order_block_demand import (
    OrderBlockDemandStrategy,
    OrderBlockDemandSubStrategy,
    OrderBookDemandStrategy,
    SmartMoneyZone,
    ZoneType,
    ZoneStatus,
    SwingPoint,
    SwingStructureDetector
)
from strategies.tick_constrained_mm import (
    TickConstrainedMMStrategy,
    TickConstrainedSubStrategy,
    TickConstrainedConfig,
    TickConstrainedSimulator,
    compute_ofi_from_trades
)

__all__ = [
    "BaseStrategy",
    "BaseSubStrategy",
    "MLStrategy",
    "MLSubStrategy",
    "OrderBlockDemandStrategy",
    "OrderBlockDemandSubStrategy",
    "OrderBookDemandStrategy",
    "SmartMoneyZone",
    "ZoneType",
    "ZoneStatus",
    "SwingPoint",
    "SwingStructureDetector",
    "EMACrossoverStrategy",
    "EMACrossoverSubStrategy",
    "EMA_PRESETS",
    "compute_ema_series",
    "StochasticRSIStrategy",
    "StochasticRSISubStrategy",
    "STOCH_RSI_PRESETS",
    "compute_rsi_series",
    "compute_stoch_rsi",
    "SmartStrategy",
    "SmartSubStrategy",
    "MarketRegime",
    "compute_chop_series",
    "BaseFilter",
    "HTFTrendFilter",
    "ADXRegimeFilter",
    "HourlySessionFilter",
    "DirectionalBiasFilter",
    "FilterPipeline",
    "compute_atr_series",
    "compute_adx_series",
    "VPINCalculator",
    "TickRunLengthDetector",
    "OrderFlowImbalance",
    "compute_bollinger_bandwidth",
    "compute_choppiness_index",
    "AdaptiveRegimeSwitcher",
    "TickConstrainedMMStrategy",
    "TickConstrainedSubStrategy",
    "TickConstrainedConfig",
    "TickConstrainedSimulator",
    "compute_ofi_from_trades",
]

