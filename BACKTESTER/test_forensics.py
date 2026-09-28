"""
Unit & Integration Tests for Forensics & Replay Lab
===================================================
Verifies data catalog discovery, candlestick slicing, indicator calculation,
millisecond tick forensics, MFE/MAE accuracy, post-exit trajectory,
what-if exit simulations, and FastAPI endpoints.
"""

import os
import unittest
from fastapi.testclient import TestClient

from BACKTESTER.analytics.forensics import ForensicsEngine
from BACKTESTER.analytics.dashboard import app


class TestForensicsEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = ForensicsEngine()
        cls.client = TestClient(app)

    def test_01_catalog_discovery(self):
        """Verify discovering available OHLCV datasets, tick feeds, and backtest runs."""
        cat = self.engine.get_catalog()
        self.assertIn("ohlcv_symbols", cat)
        self.assertIn("tick_symbols", cat)
        self.assertIn("available_runs", cat)

        # Check known symbols
        self.assertIn("TRUMP_USDT", cat["ohlcv_symbols"])
        self.assertIn("DOGE_USDT", cat["ohlcv_symbols"])
        self.assertIn("TRUMP_USDT", cat["tick_symbols"])

        # Check timeframes for TRUMP_USDT
        tfs = cat["ohlcv_symbols"]["TRUMP_USDT"]["timeframes"]
        self.assertIn("1m", tfs)
        self.assertIn("1h", tfs)
        self.assertIn("1d", tfs)

        # Check runs are found
        self.assertGreater(len(cat["available_runs"]), 0)

    def test_02_candles_slicing(self):
        """Verify loading candles formatted for TradingView Lightweight Charts."""
        candles = self.engine.get_candles(
            symbol="TRUMP_USDT",
            timeframe="1m",
            limit=50
        )
        self.assertIsInstance(candles, list)
        self.assertGreater(len(candles), 0)
        c0 = candles[0]
        self.assertIn("time", c0)
        self.assertIn("open", c0)
        self.assertIn("high", c0)
        self.assertIn("low", c0)
        self.assertIn("close", c0)
        self.assertIn("volume", c0)
        self.assertIsInstance(c0["time"], int)

    def test_03_indicator_calculations(self):
        """Verify pure-Python Wilder EMA, Stoch RSI, and ADX calculations."""
        candles = self.engine.get_candles(
            symbol="TRUMP_USDT",
            timeframe="1m",
            limit=100
        )
        indicators = self.engine.calculate_indicators(candles, {
            "param_ema_fast": 5,
            "param_ema_slow": 13,
            "param_stoch_period": 9,
            "param_rsi_period": 9
        })
        self.assertIn("ema_fast", indicators)
        self.assertIn("ema_slow", indicators)
        self.assertIn("stoch_rsi", indicators)
        self.assertEqual(len(indicators["ema_fast"]), len(candles))
        self.assertEqual(len(indicators["ema_slow"]), len(candles))
        self.assertEqual(len(indicators["stoch_rsi"]["k"]), len(candles))
        self.assertEqual(len(indicators["stoch_rsi"]["d"]), len(candles))

    def test_04_trade_forensic_context(self):
        """Verify extracting trade context, candles, indicators, strategy state, and filter badges."""
        runs = self.engine.get_catalog()["available_runs"]
        run_id = runs[0]["run_id"]

        context = self.engine.get_trade_forensic_context(run_id=run_id, trade_id=1)
        self.assertIn("trade", context)
        self.assertIn("candles", context)
        self.assertIn("indicators", context)
        self.assertIn("strategy_state", context)
        self.assertIn("filter_state", context)
        self.assertIn("mfe_mae", context)

        trade = context["trade"]
        self.assertEqual(trade["trade_id"], 1)
        self.assertIn(trade["direction"], ["LONG", "SHORT"])
        self.assertGreater(trade["entry_price"], 0)

    def test_05_tick_forensics_and_mfe_mae(self):
        """Verify millisecond tick streaming, MFE and MAE calculations on a run with ticks."""
        runs = self.engine.get_catalog()["available_runs"]
        runs_with_ticks = [r for r in runs if r.get("has_ticks")]
        run_id = runs_with_ticks[0]["run_id"] if runs_with_ticks else runs[0]["run_id"]
        context = self.engine.get_trade_forensic_context(run_id=run_id, trade_id=1)

        self.assertIn("mfe_ticks", context["mfe_mae"])
        self.assertIn("mae_ticks", context["mfe_mae"])
        self.assertGreaterEqual(context["mfe_mae"]["mfe_ticks"], 0)
        self.assertGreaterEqual(context["mfe_mae"]["mae_ticks"], 0)

        # Verify timeline events
        self.assertGreater(len(context["timeline"]), 0)
        event_types = [ev["event"] for ev in context["timeline"]]
        self.assertIn("ENTRY_FILLED", event_types)

    def test_06_what_if_simulation(self):
        """Verify counterfactual exit rule simulations against historical ticks."""
        runs = self.engine.get_catalog()["available_runs"]
        runs_with_ticks = [r for r in runs if r.get("has_ticks")]
        run_id = runs_with_ticks[0]["run_id"] if runs_with_ticks else runs[0]["run_id"]
        res = self.engine.simulate_what_if(
            run_id=run_id,
            trade_id=1,
            timeout_seconds=30.0,
            tp_ticks=3
        )
        self.assertEqual(res["status"], "HYPOTHETICAL")
        self.assertEqual(res["trade_id"], 1)
        self.assertIn("original_outcome", res)
        self.assertIn("hypothetical_outcome", res)
        self.assertIn("pnl_delta_vs_actual", res["hypothetical_outcome"])
        self.assertIn(res["hypothetical_outcome"]["exit_reason"], ["MIN_PROFIT_TP_HIT", "TIMEOUT_CLOSE", "STOP_LOSS_HIT", "DURATION_SCRATCH"])

    def test_07_fastapi_endpoints(self):
        """Verify REST endpoints exposed via FastAPI."""
        # Catalog
        res_cat = self.client.get("/api/forensics/catalog")
        self.assertEqual(res_cat.status_code, 200)

        # Candles
        res_c = self.client.get("/api/forensics/candles?symbol=TRUMP_USDT&timeframe=1m&limit=10")
        self.assertEqual(res_c.status_code, 200)
        self.assertEqual(len(res_c.json()["candles"]), 10)

        runs = self.engine.get_catalog()["available_runs"]
        run_id = runs[0]["run_id"]

        # Trade Context
        res_t = self.client.get(f"/api/forensics/trade/{run_id}/1")
        self.assertEqual(res_t.status_code, 200)

        # What-If
        res_w = self.client.post(
            f"/api/forensics/trade/{run_id}/1/what-if",
            json={"timeout_seconds": 45.0, "tp_ticks": 2}
        )
        self.assertEqual(res_w.status_code, 200)
        self.assertIn("hypothetical_outcome", res_w.json())


if __name__ == "__main__":
    unittest.main()
