"""
KCEX Automated Execution Engine Package
=======================================
Exports:
- TradeExecutionEngine: Core automated trade execution engine
- ExecutionConfig, OrderDirection, EngineMode, ExitReason: Engine models
- MasterplanStrategy, EMACrossoverStrategy, StochasticRSIStrategy, BaseStrategy: Strategies
- DualCurrencyLogger, TradeOutcomeLogger: Dual-currency loggers
- TradeOutcome, CumulativeStats: Trade recording and analytics
"""

from kcex.engine.models import (
    OrderDirection,
    ExitReason,
    EngineMode,
    TradeSignal,
    ExecutionConfig,
    TradeOutcome,
    CumulativeStats
)
from kcex.engine.logger import DualCurrencyLogger, TradeOutcomeLogger

__all__ = [
    "OrderDirection",
    "ExitReason",
    "EngineMode",
    "TradeSignal",
    "ExecutionConfig",
    "TradeOutcome",
    "CumulativeStats",
    "DualCurrencyLogger",
    "TradeOutcomeLogger",
    "BaseStrategy",
    "BaseSubStrategy",
    "EMACrossoverStrategy",
    "EMACrossoverSubStrategy",
    "EMA_PRESETS",
    "compute_ema_series",
    "StochasticRSIStrategy",
    "StochasticRSISubStrategy",
    "STOCH_RSI_PRESETS",
    "compute_stoch_rsi",
    "compute_rsi_series",
    "MasterplanStrategy",
    "TradeExecutionEngine",
]


def __getattr__(name: str):
    if name == "TradeExecutionEngine":
        from kcex.engine.executor import TradeExecutionEngine
        return TradeExecutionEngine
    if name in (
        "BaseStrategy",
        "BaseSubStrategy",
        "EMACrossoverStrategy",
        "EMACrossoverSubStrategy",
        "EMA_PRESETS",
        "compute_ema_series",
        "StochasticRSIStrategy",
        "StochasticRSISubStrategy",
        "STOCH_RSI_PRESETS",
        "compute_stoch_rsi",
        "compute_rsi_series",
        "MasterplanStrategy",
    ):
        from kcex.engine import strategy as _strat
        return getattr(_strat, name)
    raise AttributeError(f"module 'kcex.engine' has no attribute '{name}'")
