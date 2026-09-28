"""
BACKTESTER Configuration
========================
Extends ExecutionConfig with backtest-specific settings including date ranges,
data directories, initial capital, slippage, fee overrides, and playback modes.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Union

# Ensure root is in sys.path so kcex package is accessible
BACKTESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from kcex.engine.models import ExecutionConfig, OrderDirection, EngineMode


@dataclass
class BacktestConfig(ExecutionConfig):
    """
    Configuration parameters for the Backtest Engine.
    Inherits all live strategy and execution settings from ExecutionConfig.
    """
    # Dynamic timeframe (e.g. "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")
    timeframe: str = "1m"

    # Historical date range (ISO strings 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS' or ms timestamps)
    start_time: Optional[Union[str, int, float]] = None
    end_time: Optional[Union[str, int, float]] = None

    # Capital & Virtual Account
    initial_balance_usdt: float = 25000.0
    inr_rate: float = 94.45

    # Data Directories
    ohlcv_data_dir: str = (
        r"D:\My_Bots\Trading\BINANCE_DATA\OHLCV_Data\binance_futures_ohlcv"
        if os.path.isdir(r"D:\My_Bots\Trading\BINANCE_DATA\OHLCV_Data\binance_futures_ohlcv")
        else (
            r"D:\My_Bots\Trading\KCEX\BACKTESTER\OHLCV_Data_Binance"
            if os.path.isdir(r"D:\My_Bots\Trading\KCEX\BACKTESTER\OHLCV_Data_Binance")
            else os.path.join(BACKTESTER_DIR, "OHLCV_Data_Binance")
        )
    )
    trades_data_dir: str = (
        r"D:\My_Bots\Trading\BINANCE_DATA\Tick_Trades_Data\binance_futures_trades"
        if os.path.isdir(r"D:\My_Bots\Trading\BINANCE_DATA\Tick_Trades_Data\binance_futures_trades")
        else (
            r"D:\My_Bots\Trading\KCEX\BACKTESTER\Historical_Trades_Data_Binance"
            if os.path.isdir(r"D:\My_Bots\Trading\KCEX\BACKTESTER\Historical_Trades_Data_Binance")
            else os.path.join(BACKTESTER_DIR, "Historical_Trades_Data_Binance")
        )
    )
    reports_dir: str = os.path.join(BACKTESTER_DIR, "reports")

    # High-fidelity Simulation Options
    use_tick_data: bool = True               # Stream tick-by-tick trades when available for active trades
    tick_fallback_to_candle: bool = True     # Use candle high/low if tick trade file is missing for a slice
    slippage_ticks: int = 0                  # Additional adverse fill slippage in ticks
    fee_mode: str = "LIVE"                   # "LIVE", "ZERO", or "MANUAL"
    maker_fee_override: Optional[float] = None
    taker_fee_override: Optional[float] = None

    # Playback pacing
    # 0.0 = batch processing (as fast as CPU allows)
    # >0.0 = simulated realtime playback speed multiplier (e.g. 10.0 = 10x real-time speed)
    playback_speed: float = 0.0

    # Logging & Display
    verbose_ticks: bool = False              # Print every single tick while in position (only recommended for slow playback)
    show_progress: bool = True

    def __post_init__(self):
        # Force mode to DRY_RUN / simulation
        self.mode = EngineMode.DRY_RUN
        # Keep interval in sync with timeframe for strategy logic
        self.ema_interval = self.timeframe
        self.stoch_interval = self.timeframe
        self.smart_interval = self.timeframe
        # Normalize symbol name (e.g. TRUMP_USDT -> TRUMP_USDT)
        if self.symbol:
            self.symbol = self.symbol.upper()
            if "DOGE" in self.symbol and self.volume_multiplier == 2.0 and self.volume_mode == "MULTIPLIER":
                self.volume_multiplier = 1.0

        # Ensure directory paths are resolved correctly regardless of cwd
        for attr, folder_name in [
            ("ohlcv_data_dir", "OHLCV_Data_Binance"),
            ("trades_data_dir", "Historical_Trades_Data_Binance"),
            ("reports_dir", "reports")
        ]:
            val = getattr(self, attr, None)
            if val and not os.path.isabs(val) and not os.path.exists(val):
                candidate = os.path.join(BACKTESTER_DIR, os.path.basename(val))
                if os.path.exists(candidate) or attr == "reports_dir":
                    setattr(self, attr, candidate)
                else:
                    setattr(self, attr, os.path.join(ROOT_DIR, val))
