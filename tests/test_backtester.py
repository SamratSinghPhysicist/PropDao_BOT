"""
Unit tests for PropDAO Backtester runner and reporting.
"""

import os
import shutil
import tempfile
import unittest
from backtester.config import BacktestConfig
from backtester.data_loader import DataLoader
from backtester.runner import BacktestRunner


class TestBacktester(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.temp_dir, "data")
        self.reports_dir = os.path.join(self.temp_dir, "reports")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)

        # Generate 150 synthetic candles
        self.candles = []
        base_price = 50000.0
        start_ts = 1767225600000  # 2026-01-01 00:00:00 UTC

        for i in range(150):
            # Oscillating waves with swing highs and lows
            cycle = (i % 20)
            if cycle < 10:
                o = base_price + cycle * 50
                c = o + 40
                h = c + 20
                l = o - 20
            else:
                o = base_price + (20 - cycle) * 50
                c = o - 40
                h = o + 20
                l = c - 20

            self.candles.append({
                "timestamp": start_ts + (i * 900000),  # 15m step
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 10.0
            })

        # Save synthetic candles to cache file
        target_dir = os.path.join(self.data_dir, "BTCUSDC", "15m")
        os.makedirs(target_dir, exist_ok=True)
        cache_path = os.path.join(target_dir, "BTCUSDC_15m_20260101_20260105.csv")
        DataLoader._save_csv(cache_path, self.candles)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_backtest_runner_pipeline(self):
        config = BacktestConfig(
            symbol="BTCUSDC",
            timeframe="15m",
            start_date="2026-01-01",
            end_date="2026-01-05",
            initial_balance=25000.0,
            leverage=2.0,
            risk_fraction_per_trade=0.25,
            pivot_len=3,
            data_dir=self.data_dir,
            reports_dir=self.reports_dir
        )

        runner = BacktestRunner(config)
        metrics, artifacts = runner.run()

        self.assertIsNotNone(metrics)
        self.assertIn("csv", artifacts)
        self.assertIn("jsonl", artifacts)
        self.assertIn("markdown", artifacts)

        self.assertTrue(os.path.exists(artifacts["csv"]))
        self.assertTrue(os.path.exists(artifacts["jsonl"]))
        self.assertTrue(os.path.exists(artifacts["markdown"]))


if __name__ == "__main__":
    unittest.main()
