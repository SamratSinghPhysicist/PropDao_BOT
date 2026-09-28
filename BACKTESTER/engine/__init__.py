"""
BACKTESTER Engine Package
=========================
A modular dual-feed backtester that reuses live trading strategy and execution logic.
"""

from BACKTESTER.engine.config import BacktestConfig
from BACKTESTER.engine.scanner import DataScanner, SymbolDataCatalog, canonicalize_symbol
from BACKTESTER.engine.data_loader import OHLCVLoader, TickTradeStreamer, Candle, TradeTick
from BACKTESTER.engine.market_sim import BacktestMarket
from BACKTESTER.engine.execution_sim import BacktestExecutionEngine, VirtualClock, EquityPoint
from BACKTESTER.engine.metrics import PerformanceCalculator, PerformanceSummary
from BACKTESTER.engine.reporting import BacktestReporter

__all__ = [
    "BacktestConfig",
    "DataScanner",
    "SymbolDataCatalog",
    "canonicalize_symbol",
    "OHLCVLoader",
    "TickTradeStreamer",
    "Candle",
    "TradeTick",
    "BacktestMarket",
    "BacktestExecutionEngine",
    "VirtualClock",
    "EquityPoint",
    "PerformanceCalculator",
    "PerformanceSummary",
    "BacktestReporter"
]
