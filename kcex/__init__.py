"""
KCEX Trading Bot Package
========================
A comprehensive Python toolkit for automated futures trading and trade management on KCEX.

Exports:
- KCEXConfig: API endpoints and credentials configuration
- KCEXClient, KCEXAPIError: Core HTTP client with session signing
- KCEXMarket, ContractInfo: Real-time market data, contract specs, depth, klines
- KCEXRiskCalculator, RiskAnalysisReport: Liquidation, fees, TP/SL, and dual currency (USDT & INR)
- KCEXTrader: Order creation, position TP/SL, partial closing, cancellations
- KCEXSigner: Reverse-engineered MD5 request signing
- MasterplanStrategy, EMACrossoverStrategy, StochasticRSIStrategy, BaseStrategy: Modular strategies
"""

from kcex.config import KCEXConfig
from kcex.signer import KCEXSigner
from kcex.client import KCEXClient, KCEXAPIError
from kcex.market import KCEXMarket, ContractInfo
from kcex.risk import KCEXRiskCalculator, RiskAnalysisReport
from kcex.trade import KCEXTrader
from kcex.feed import KCEXWebSocketFeed
from kcex.engine.models import (
    ExecutionConfig,
    OrderDirection,
    EngineMode,
    ExitReason,
    TradeSignal,
    TradeOutcome,
    CumulativeStats
)
from kcex.engine.logger import DualCurrencyLogger, TradeOutcomeLogger

__version__ = "1.4.0"

__all__ = [
    "KCEXConfig",
    "KCEXSigner",
    "KCEXClient",
    "KCEXAPIError",
    "KCEXMarket",
    "ContractInfo",
    "KCEXRiskCalculator",
    "RiskAnalysisReport",
    "KCEXTrader",
    "KCEXWebSocketFeed",
    "TradeExecutionEngine",
    "ExecutionConfig",
    "OrderDirection",
    "EngineMode",
    "ExitReason",
    "TradeSignal",
    "TradeOutcome",
    "CumulativeStats",
    "BaseStrategy",
    "BaseSubStrategy",
    "MasterplanStrategy",
    "EMACrossoverStrategy",
    "EMACrossoverSubStrategy",
    "EMA_PRESETS",
    "compute_ema_series",
    "StochasticRSIStrategy",
    "StochasticRSISubStrategy",
    "STOCH_RSI_PRESETS",
    "compute_stoch_rsi",
    "compute_rsi_series",
    "DualCurrencyLogger",
    "TradeOutcomeLogger",
]


def __getattr__(name: str):
    if name in (
        "TradeExecutionEngine",
        "BaseStrategy",
        "BaseSubStrategy",
        "MasterplanStrategy",
        "EMACrossoverStrategy",
        "EMACrossoverSubStrategy",
        "EMA_PRESETS",
        "compute_ema_series",
        "StochasticRSIStrategy",
        "StochasticRSISubStrategy",
        "STOCH_RSI_PRESETS",
        "compute_stoch_rsi",
        "compute_rsi_series",
    ):
        from kcex import engine as _engine
        return getattr(_engine, name)
    raise AttributeError(f"module 'kcex' has no attribute '{name}'")
