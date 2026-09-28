"""
Comprehensive Unit & Integration Test Suite for BACKTESTER
===========================================================
Tests dynamic data scanning, streaming tick readers, candle feeding,
virtual market clock, strategy execution, and metric calculations.
"""

import os
import sys
import unittest

# Ensure project root is in path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.config import BacktestConfig
from BACKTESTER.engine.scanner import DataScanner, canonicalize_symbol
from BACKTESTER.engine.data_loader import OHLCVLoader, TickTradeStreamer, Candle, TradeTick
from BACKTESTER.engine.market_sim import BacktestMarket
from BACKTESTER.engine.execution_sim import BacktestExecutionEngine, VirtualClock
from BACKTESTER.engine.metrics import PerformanceCalculator
from kcex.engine.models import OrderDirection, ExitReason


class TestBacktesterSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.scanner = DataScanner()
        cls.ohlcv_loader = OHLCVLoader()
        cls.tick_streamer = TickTradeStreamer()
        cls.catalog = cls.scanner.scan()

    def test_symbol_canonicalization(self):
        self.assertEqual(canonicalize_symbol("TRUMPUSDT"), "TRUMP_USDT")
        self.assertEqual(canonicalize_symbol("trump_usdt"), "TRUMP_USDT")
        self.assertEqual(canonicalize_symbol("DOGEUSDT"), "DOGE_USDT")
        self.assertEqual(canonicalize_symbol("BTC_USDT"), "BTC_USDT")

    def test_data_scanner_discovery(self):
        catalog = self.catalog
        self.assertIn("TRUMP_USDT", catalog)
        self.assertIn("DOGE_USDT", catalog)

        trump_data = catalog["TRUMP_USDT"]
        self.assertTrue(trump_data.has_ohlcv)
        self.assertTrue(trump_data.has_trades)
        self.assertIn("1m", trump_data.ohlcv_timeframes)

        ov_start, ov_end = trump_data.get_overlap_range()
        self.assertIsNotNone(ov_start)
        self.assertIsNotNone(ov_end)
        self.assertTrue(ov_start < ov_end)

    def test_ohlcv_loader(self):
        # Load a 1-day slice of 1m candles for TRUMP_USDT (July 1, 2026)
        start_ms = 1782864000000 # 2026-07-01 00:00:00
        end_ms = start_ms + (3600 * 1000) # 1 hour = 60 candles
        candles = self.ohlcv_loader.load_candles(
            symbol="TRUMP_USDT",
            timeframe="1m",
            start_ms=start_ms,
            end_ms=end_ms
        )
        self.assertGreater(len(candles), 0)
        first = candles[0]
        self.assertIsInstance(first, Candle)
        self.assertGreater(first.close, 0.0)
        self.assertGreater(first.high, 0.0)

    def test_tick_streamer(self):
        # Stream first 10 ticks for TRUMP_USDT starting from July 1, 2026
        start_ms = 1782864000000
        ticks = []
        for tick in self.tick_streamer.stream_ticks("TRUMP_USDT", start_ms=start_ms):
            ticks.append(tick)
            if len(ticks) >= 10:
                break
        self.assertEqual(len(ticks), 10)
        self.assertIsInstance(ticks[0], TradeTick)
        self.assertGreater(ticks[0].price, 0.0)
        self.assertGreaterEqual(ticks[0].timestamp_ms, start_ms)

    def test_backtest_market_no_lookahead(self):
        market = BacktestMarket()
        # Seed 5 mock candles
        mock_candles = [
            Candle(open_time_ms=1000 * i, open=10.0 + i, high=11.0 + i, low=9.0 + i, close=10.5 + i, volume=100, close_time_ms=1000 * i + 999)
            for i in range(10)
        ]
        market.set_candles("TRUMP_USDT", "1m", mock_candles)

        # Set clock to timestamp 3000
        market.set_time(3000, current_price=13.5)
        klines = market.get_klines("TRUMP_USDT", interval="Min1", limit=10)
        # Should only return candles whose open_time_ms <= 3000 (i.e. indices 0, 1, 2, 3)
        self.assertEqual(len(klines), 4)
        self.assertEqual(klines[-1]["timestamp_ms"], 3000)

    def test_full_backtest_execution(self):
        # Run a small backtest on TRUMP_USDT for 3 days with max_trades=3
        config = BacktestConfig(
            symbol="TRUMP_USDT",
            timeframe="1m",
            strategy_mode="EMA_CROSSOVER",
            ema_preset="5/13",
            start_time="2026-07-01 00:00:00",
            end_time="2026-07-03 00:00:00",
            tp_ticks=2,
            sl_mode="TICKS",
            sl_ticks=10,
            leverage=30,
            max_trades=3,
            initial_balance_usdt=100.0,
            use_tick_data=True
        )

        engine = BacktestExecutionEngine(config=config)
        outcomes = engine.run()

        self.assertGreater(len(outcomes), 0)
        self.assertLessEqual(len(outcomes), 3)

        outcome = outcomes[0]
        self.assertIn(outcome.exit_reason, [
            ExitReason.MIN_PROFIT_TP_HIT,
            ExitReason.STOP_LOSS_HIT,
            ExitReason.IMMEDIATE_PROFIT_CLOSE,
            ExitReason.MANUAL_CLOSE
        ])
        self.assertGreater(outcome.entry_price, 0.0)
        self.assertGreater(outcome.exit_price, 0.0)
        self.assertGreater(outcome.margin_used_usdt, 0.0)

        # Verify performance metrics calculation
        summary = PerformanceCalculator.calculate(outcomes, initial_balance_usdt=100.0)
        self.assertEqual(summary.total_trades, len(outcomes))
        self.assertGreaterEqual(summary.win_rate_pct, 0.0)
        self.assertLessEqual(summary.win_rate_pct, 100.0)

    def test_full_backtest_execution_ohlcv_only(self):
        # Run backtest with use_tick_data=False (OHLCV only mode)
        config = BacktestConfig(
            symbol="TRUMP_USDT",
            timeframe="1m",
            strategy_mode="EMA_CROSSOVER",
            ema_preset="5/13",
            start_time="2026-07-01 00:00:00",
            end_time="2026-07-03 00:00:00",
            tp_ticks=2,
            sl_mode="TICKS",
            sl_ticks=10,
            leverage=30,
            max_trades=3,
            initial_balance_usdt=100.0,
            use_tick_data=False
        )

        engine = BacktestExecutionEngine(config=config)
        outcomes = engine.run()

        self.assertGreater(len(outcomes), 0)
        self.assertLessEqual(len(outcomes), 3)

        outcome = outcomes[0]
        self.assertIn(outcome.exit_reason, [
            ExitReason.MIN_PROFIT_TP_HIT,
            ExitReason.STOP_LOSS_HIT,
            ExitReason.IMMEDIATE_PROFIT_CLOSE,
            ExitReason.MANUAL_CLOSE
        ])
        self.assertGreater(outcome.entry_price, 0.0)
        self.assertGreater(outcome.exit_price, 0.0)


if __name__ == "__main__":
    unittest.main()
