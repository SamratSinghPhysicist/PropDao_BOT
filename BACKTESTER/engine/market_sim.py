"""
Backtest Market Simulation Adapter
===================================
Provides a 100% faithful emulation of the KCEXMarket interface, allowing
all live trading strategies (EMA Crossover, Stochastic RSI, Directional Cycle,
Microstructure, and MasterplanStrategy) to execute without modification or lookahead bias.
"""

import os
import sys
from typing import Dict, List, Any, Optional

# Ensure project root is in path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from kcex.market import ContractInfo
from BACKTESTER.engine.scanner import canonicalize_symbol
from BACKTESTER.engine.data_loader import Candle, normalize_timeframe, timeframe_to_kcex_interval


# Preconfigured contract metadata for major pairs (fallback/default values matching exchange specs)
DEFAULT_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "TRUMP_USDT": {
        "base_coin": "TRUMP",
        "quote_coin": "USDT",
        "contract_size": 0.1,
        "price_unit": 0.001,
        "volume_unit": 1.0,
        "price_precision": 3,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.001"]
    },
    "DOGE_USDT": {
        "base_coin": "DOGE",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.00001,
        "volume_unit": 1.0,
        "price_precision": 5,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 100,
        "maintenance_margin_ratio": 0.005,
        "initial_margin_ratio": 0.01,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.00001"]
    },
    "BTC_USDT": {
        "base_coin": "BTC",
        "quote_coin": "USDT",
        "contract_size": 0.0001,
        "price_unit": 0.1,
        "volume_unit": 1.0,
        "price_precision": 1,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 125,
        "maintenance_margin_ratio": 0.004,
        "initial_margin_ratio": 0.008,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.1"]
    },
    "BTC_USDC": {
        "base_coin": "BTC",
        "quote_coin": "USDC",
        "contract_size": 0.0001,
        "price_unit": 0.1,
        "volume_unit": 1.0,
        "price_precision": 1,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 125,
        "maintenance_margin_ratio": 0.004,
        "initial_margin_ratio": 0.008,
        "maker_fee_rate": 0.00015,
        "taker_fee_rate": 0.00045,
        "depth_steps": ["0.1"]
    },
    "ETH_USDT": {
        "base_coin": "ETH",
        "quote_coin": "USDT",
        "contract_size": 0.001,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 100,
        "maintenance_margin_ratio": 0.005,
        "initial_margin_ratio": 0.01,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.01"]
    },
    "ETH_USDC": {
        "base_coin": "ETH",
        "quote_coin": "USDC",
        "contract_size": 0.001,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 100,
        "maintenance_margin_ratio": 0.005,
        "initial_margin_ratio": 0.01,
        "maker_fee_rate": 0.00015,
        "taker_fee_rate": 0.00045,
        "depth_steps": ["0.01"]
    },
    "SOL_USDT": {
        "base_coin": "SOL",
        "quote_coin": "USDT",
        "contract_size": 0.01,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.01"]
    },
    "SOL_USDC": {
        "base_coin": "SOL",
        "quote_coin": "USDC",
        "contract_size": 0.01,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.00015,
        "taker_fee_rate": 0.00045,
        "depth_steps": ["0.01"]
    },
    "XRP_USDT": {
        "base_coin": "XRP",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.0001"]
    },
    "PEPE_USDT": {
        "base_coin": "PEPE",
        "quote_coin": "USDT",
        "contract_size": 100000.0,
        "price_unit": 0.0000001,
        "volume_unit": 1.0,
        "price_precision": 7,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 50000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.0000001"]
    },
    "BNB_USDT": {
        "base_coin": "BNB",
        "quote_coin": "USDT",
        "contract_size": 0.01,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.01"]
    },
    "SUI_USDT": {
        "base_coin": "SUI",
        "quote_coin": "USDT",
        "contract_size": 1.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.0001"]
    },
    "XAU_USDT": {
        "base_coin": "XAU",
        "quote_coin": "USDT",
        "contract_size": 0.001,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 100,
        "maintenance_margin_ratio": 0.005,
        "initial_margin_ratio": 0.01,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.01"]
    },
    "CL_USDT": {
        "base_coin": "CL",
        "quote_coin": "USDT",
        "contract_size": 0.01,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.01"]
    },
    "XAG_USDT": {
        "base_coin": "XAG",
        "quote_coin": "USDT",
        "contract_size": 0.01,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.01"]
    },
    "1000000MOG_USDT": {
        "base_coin": "1000000MOG",
        "quote_coin": "USDT",
        "contract_size": 1.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 10000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0001,
        "depth_steps": ["0.0001"]
    },
    "MOG_USDT": {
        "base_coin": "1000000MOG",
        "quote_coin": "USDT",
        "contract_size": 1.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 10000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "MELANIA_USDT": {
        "base_coin": "MELANIA",
        "quote_coin": "USDT",
        "contract_size": 0.1,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "DOGS_USDT": {
        "base_coin": "DOGS",
        "quote_coin": "USDT",
        "contract_size": 1000.0,
        "price_unit": 0.000001,
        "volume_unit": 1.0,
        "price_precision": 6,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 10000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.000001"]
    },
    "MEME_USDT": {
        "base_coin": "MEME",
        "quote_coin": "USDT",
        "contract_size": 100.0,
        "price_unit": 0.00001,
        "volume_unit": 1.0,
        "price_precision": 5,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 10000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.00001"]
    },
    "BOME_USDT": {
        "base_coin": "BOME",
        "quote_coin": "USDT",
        "contract_size": 100.0,
        "price_unit": 0.00001,
        "volume_unit": 1.0,
        "price_precision": 5,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 10000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.00001"]
    },
    "ACT_USDT": {
        "base_coin": "ACT",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 25,
        "maintenance_margin_ratio": 0.02,
        "initial_margin_ratio": 0.04,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "AVAAI_USDT": {
        "base_coin": "AVAAI",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 25,
        "maintenance_margin_ratio": 0.02,
        "initial_margin_ratio": 0.04,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "CHILLGUY_USDT": {
        "base_coin": "CHILLGUY",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 25,
        "maintenance_margin_ratio": 0.02,
        "initial_margin_ratio": 0.04,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "GOAT_USDT": {
        "base_coin": "GOAT",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 25,
        "maintenance_margin_ratio": 0.02,
        "initial_margin_ratio": 0.04,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "PIPPIN_USDT": {
        "base_coin": "PIPPIN",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 25,
        "maintenance_margin_ratio": 0.02,
        "initial_margin_ratio": 0.04,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "WIF_USDT": {
        "base_coin": "WIF",
        "quote_coin": "USDT",
        "contract_size": 1.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "KOMA_USDT": {
        "base_coin": "KOMA",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 25,
        "maintenance_margin_ratio": 0.02,
        "initial_margin_ratio": 0.04,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "AIXBT_USDT": {
        "base_coin": "AIXBT",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.0001,
        "volume_unit": 1.0,
        "price_precision": 4,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 50,
        "maintenance_margin_ratio": 0.01,
        "initial_margin_ratio": 0.02,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "depth_steps": ["0.0001"]
    },
    "XMR_USDT": {
        "base_coin": "XMR",
        "quote_coin": "USDT",
        "contract_size": 0.01,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0002,
        "depth_steps": ["0.01"]
    },
    "AVAX_USDT": {
        "base_coin": "AVAX",
        "quote_coin": "USDT",
        "contract_size": 0.1,
        "price_unit": 0.001,
        "volume_unit": 1.0,
        "price_precision": 3,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0002,
        "depth_steps": ["0.001"]
    },
    "TRX_USDT": {
        "base_coin": "TRX",
        "quote_coin": "USDT",
        "contract_size": 10.0,
        "price_unit": 0.00001,
        "volume_unit": 1.0,
        "price_precision": 5,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 5000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0002,
        "depth_steps": ["0.00001"]
    },
    "HYPE_USDT": {
        "base_coin": "HYPE",
        "quote_coin": "USDT",
        "contract_size": 0.1,
        "price_unit": 0.001,
        "volume_unit": 1.0,
        "price_precision": 3,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0002,
        "depth_steps": ["0.001"]
    },
    "LTC_USDT": {
        "base_coin": "LTC",
        "quote_coin": "USDT",
        "contract_size": 0.01,
        "price_unit": 0.01,
        "volume_unit": 1.0,
        "price_precision": 2,
        "volume_precision": 0,
        "min_volume": 1.0,
        "max_volume": 1000000.0,
        "min_leverage": 1,
        "max_leverage": 75,
        "maintenance_margin_ratio": 0.0067,
        "initial_margin_ratio": 0.0133,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0002,
        "depth_steps": ["0.01"]
    }
}


class BacktestMarket:
    """
    Virtual KCEXMarket adapter backed by historical datasets.
    Controls the simulation timeline and delivers kline snapshots up to the current clock.
    """

    def __init__(
        self,
        inr_rate: float = 94.45,
        fee_mode: str = "LIVE",
        maker_fee_override: Optional[float] = None,
        taker_fee_override: Optional[float] = None
    ):
        self.inr_rate = inr_rate
        self.fee_mode = fee_mode.upper() if fee_mode else "LIVE"
        self.maker_fee_override = maker_fee_override
        self.taker_fee_override = taker_fee_override

        # Active simulation clock
        self.current_time_ms: int = 0
        self.current_price: float = 0.0
        self.current_bid: float = 0.0
        self.current_ask: float = 0.0

        # Cached candles indexed by (symbol, timeframe)
        self._candle_cache: Dict[str, List[Candle]] = {}
        # Precomputed list of timestamps for fast binary search slicing
        self._candle_timestamps: Dict[str, List[int]] = {}

        # Cached ContractInfo instances
        self._contracts: Dict[str, ContractInfo] = {}

    def set_candles(self, symbol: str, timeframe: str, candles: List[Candle]) -> None:
        """Seeds the historical candles for a symbol and timeframe."""
        canonical = canonicalize_symbol(symbol)
        key = f"{canonical}:{normalize_timeframe(timeframe)}"
        self._candle_cache[key] = candles
        self._candle_timestamps[key] = [c.open_time_ms for c in candles]

        # Auto-initialize current price if not set
        if candles and self.current_price == 0.0:
            self.current_price = candles[0].close
            pu = self.get_contract_detail(canonical).price_unit
            self.current_bid = self.current_price - (0.5 * pu)
            self.current_ask = self.current_price + (0.5 * pu)

    def set_current_price(self, price: float, pu: float = 0.001):
        self.current_price = price
        self.current_bid = round(price - pu, 6)
        self.current_ask = round(price + pu, 6)

    def advance_clock(self, timestamp_ms: int):
        self.current_time_ms = timestamp_ms

    def register_candles(self, symbol: str, timeframe: str, candles: List[Candle]):
        norm_tf = normalize_timeframe(timeframe)
        canonical = canonicalize_symbol(symbol)
        key = f"{canonical}_{norm_tf}"
        key_colon = f"{canonical}:{norm_tf}"
        self._candle_cache[key] = candles
        self._candle_cache[key_colon] = candles
        self._candle_timestamps[key] = [c.open_time_ms for c in candles]
        self._candle_timestamps[key_colon] = [c.open_time_ms for c in candles]
        if candles:
            last = candles[-1]
            self.current_price = last.close
            pu = self.get_contract_detail(symbol).price_unit
            self.current_bid = round(last.close - pu, 6)
            self.current_ask = round(last.close + pu, 6)

    def set_time(self, timestamp_ms: int, current_price: Optional[float] = None, bid: Optional[float] = None, ask: Optional[float] = None) -> None:
        """Advances the virtual market clock."""
        self.current_time_ms = timestamp_ms
        if current_price is not None:
            self.current_price = current_price
        if bid is not None:
            self.current_bid = bid
        elif current_price is not None:
            self.current_bid = current_price
        if ask is not None:
            self.current_ask = ask
        elif current_price is not None:
            self.current_ask = current_price

    def ping(self) -> bool:
        return True

    def get_inr_rate(self) -> float:
        return self.inr_rate

    def get_fiat_exchange_rates(self) -> Dict[str, float]:
        return {"INR": self.inr_rate, "USD": 1.0}

    def get_contract_detail(self, symbol: str) -> ContractInfo:
        """Retrieves or synthesizes ContractInfo for the given symbol."""
        canonical = canonicalize_symbol(symbol)
        if canonical in self._contracts:
            return self._contracts[canonical]

        # 1. Attempt to fetch live contract metadata from KCEX if reachable
        live_info: Optional[ContractInfo] = None
        try:
            from kcex.market import KCEXMarket
            k_market = KCEXMarket()
            live_info = k_market.get_contract_detail(canonical)
        except Exception:
            live_info = None

        if live_info is not None:
            base_coin = live_info.base_coin
            quote_coin = live_info.quote_coin
            cs = live_info.contract_size
            pu = live_info.price_unit
            vu = live_info.volume_unit
            ps = live_info.price_precision
            # Scale adjustment for 1000000MOG: Binance candles are priced per 1M MOG (~$0.12)
            if "MOG" in canonical:
                cs = 1.0
                pu = 0.0001
                ps = 4
            vs = live_info.volume_precision
            min_v = live_info.min_volume
            max_v = live_info.max_volume
            min_l = live_info.min_leverage
            max_l = live_info.max_leverage
            mmr = live_info.maintenance_margin_ratio
            imr = live_info.initial_margin_ratio
            mfr = live_info.maker_fee_rate
            tfr = live_info.taker_fee_rate
            depth_steps = live_info.depth_steps
            raw_data = live_info.raw_data
        else:
            # Fallback to preconfigured template or dynamic estimator
            template = DEFAULT_CONTRACTS.get(canonical, {})
            base_coin = template.get("base_coin", canonical.split("_")[0])
            quote_coin = template.get("quote_coin", "USDT")
            if canonical in DEFAULT_CONTRACTS:
                cs = float(template.get("contract_size", 1.0))
                pu = float(template.get("price_unit", 0.001))
                ps = int(template.get("price_precision", 3))
            else:
                sample_p = self.current_price
                if not sample_p or sample_p <= 0:
                    for k, c_list in self._candle_cache.items():
                        if k.startswith(canonical) and c_list:
                            sample_p = c_list[-1].close
                            break
                sample_p = sample_p or 1.0

                # Derive realistic contract size (aiming for 1 contract ~ 5 to 50 USDT notional)
                if sample_p >= 10000:
                    cs = 0.001
                    pu = 0.1
                    ps = 1
                elif sample_p >= 1000:
                    cs = 0.01
                    pu = 0.01
                    ps = 2
                elif sample_p >= 100:
                    cs = 0.1
                    pu = 0.01
                    ps = 2
                elif sample_p >= 1:
                    cs = 1.0
                    pu = 0.001
                    ps = 3
                elif sample_p >= 0.01:
                    cs = 10.0
                    pu = 0.0001
                    ps = 4
                else:
                    cs = 100000.0
                    pu = 0.0000001
                    ps = 7

            vu = float(template.get("volume_unit", 1.0))
            vs = int(template.get("volume_precision", 0))
            min_v = float(template.get("min_volume", 1.0))
            max_v = float(template.get("max_volume", 1000000.0))
            min_l = int(template.get("min_leverage", 1))
            max_l = int(template.get("max_leverage", 75))
            mmr = float(template.get("maintenance_margin_ratio", 0.0067))
            imr = float(template.get("initial_margin_ratio", 0.0133))
            is_zero_fee_pair = any(k in canonical for k in ("TRUMP", "DOGE"))
            if is_zero_fee_pair:
                mfr = 0.0
                tfr = 0.0
            else:
                mfr = 0.0
                tfr = 0.0001  # 0.01% taker fee
            depth_steps = template.get("depth_steps", [str(pu)])
            raw_data = {}

        # 2. Apply fee_mode policies
        if self.fee_mode == "ZERO":
            mfr = 0.0
            tfr = 0.0
        elif self.fee_mode == "MANUAL":
            mfr = self.maker_fee_override if self.maker_fee_override is not None else 0.0
            tfr = self.taker_fee_override if self.taker_fee_override is not None else 0.0
        elif self.fee_mode == "LIVE":
            is_zero_fee_pair = any(k in canonical for k in ("TRUMP", "DOGE"))
            if is_zero_fee_pair:
                mfr = 0.0
                tfr = 0.0
            else:
                mfr = 0.0
                tfr = 0.0001  # 0.01% taker (no maker)

        # Specific manual overrides always take ultimate precedence
        if self.maker_fee_override is not None:
            mfr = self.maker_fee_override
        if self.taker_fee_override is not None:
            tfr = self.taker_fee_override

        info = ContractInfo(
            symbol=canonical,
            base_coin=base_coin,
            quote_coin=quote_coin,
            contract_size=cs,
            price_unit=pu,
            volume_unit=vu,
            price_precision=ps,
            volume_precision=vs,
            min_volume=min_v,
            max_volume=max_v,
            min_leverage=min_l,
            max_leverage=max_l,
            maintenance_margin_ratio=mmr,
            initial_margin_ratio=imr,
            maker_fee_rate=mfr,
            taker_fee_rate=tfr,
            depth_steps=depth_steps,
            raw_data=raw_data
        )
        self._contracts[canonical] = info
        return info

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Returns virtual ticker at the current simulation clock."""
        canonical = canonicalize_symbol(symbol)
        contract = self.get_contract_detail(canonical)
        pu = contract.price_unit
        price = self.current_price or 1.0
        bid = self.current_bid or (price - (0.5 * pu))
        ask = self.current_ask or (price + (0.5 * pu))

        return {
            "symbol": canonical,
            "lastPrice": price,
            "fairPrice": price,
            "indexPrice": price,
            "bid1": bid,
            "ask1": ask,
            "high24Price": price,
            "lower24Price": price,
            "volume24": 0.0
        }

    def get_order_book(self, symbol: str, step: Optional[str] = None) -> Dict[str, List[List[float]]]:
        """Synthesizes order book depth centered around current price."""
        canonical = canonicalize_symbol(symbol)
        contract = self.get_contract_detail(canonical)
        pu = contract.price_unit
        mid = self.current_price or 1.0

        bids = [[round(mid - (i * pu), contract.price_precision), 100.0, 5] for i in range(1, 11)]
        asks = [[round(mid + (i * pu), contract.price_precision), 100.0, 5] for i in range(1, 11)]
        return {"bids": bids, "asks": asks, "version": 1}

    def get_klines(
        self,
        symbol: str,
        interval: str = "Min1",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Returns klines up to current_time_ms strictly, preventing any lookahead bias.
        """
        canonical = canonicalize_symbol(symbol)
        norm_tf = normalize_timeframe(interval)
        key = f"{canonical}:{norm_tf}"

        candles = self._candle_cache.get(key, [])
        if not candles:
            # Fallback to 1m if requested timeframe not seeded
            key = f"{canonical}:1m"
            candles = self._candle_cache.get(key, [])
            if not candles:
                return []

        ts_list = self._candle_timestamps.get(key, [])

        # Find candles whose open_time_ms <= current_time_ms
        # Binary search for index
        import bisect
        cutoff_ms = self.current_time_ms if self.current_time_ms > 0 else (candles[-1].open_time_ms + 1)
        idx = bisect.bisect_right(ts_list, cutoff_ms)

        # Slice up to limit candles
        start_idx = max(0, idx - limit)
        selected = candles[start_idx:idx]

        return [c.to_kcex_dict() for c in selected]
