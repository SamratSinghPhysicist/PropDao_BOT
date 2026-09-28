"""
Unit Test Suite for Candle Disambiguation & Discrepancy Resolution
==================================================================
Tests:
1. Pure TP hit on candle
2. Pure SL hit on candle
3. Multi-minute candle with simultaneous TP & SL resolved via 1m sub-candles (TP first)
4. Multi-minute candle with simultaneous TP & SL resolved via 1m sub-candles (SL first)
5. Multi-minute candle where 1m sub-candle also hits both -> declares SL
6. 1m candle hitting both TP & SL -> declares SL
7. Candle open immediate breach clarification (gap at open)
8. Short position disambiguation
"""

import os
import sys
import unittest

# Ensure project root is in sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.config import BacktestConfig
from BACKTESTER.engine.data_loader import Candle
from BACKTESTER.engine.execution_sim import BacktestExecutionEngine
from kcex.engine.models import OrderDirection, ExitReason


class TestCandleDisambiguation(unittest.TestCase):

    def setUp(self):
        self.config = BacktestConfig(
            symbol="TRUMP_USDT",
            timeframe="5m",
            use_tick_data=False,
            tp_ticks=5,
            sl_mode="TICKS",
            sl_ticks=5,
            leverage=20,
            initial_balance_usdt=100.0
        )
        self.engine = BacktestExecutionEngine(config=self.config)

    def test_open_immediate_breach(self):
        # Long position: entry = 10.0, TP = 10.005, SL = 9.995 (pu = 0.001)
        entry_price = 10.0
        exact_tp = 10.005
        exact_sl = 9.995
        pu = 0.001
        ps = 3

        # Candle opens above TP
        c_tp_open = Candle(
            open_time_ms=1000,
            open=10.006,
            high=10.010,
            low=9.990,
            close=10.000,
            volume=100,
            close_time_ms=60000
        )
        p, reason, t = self.engine._resolve_candle_exit_order(
            c=c_tp_open,
            direction=OrderDirection.LONG,
            entry_price=entry_price,
            exact_tp=exact_tp,
            exact_sl=exact_sl,
            pu=pu,
            ps=ps,
            apply_slip=False,
            slippage_ticks=0,
            initial_sl=exact_sl
        )
        self.assertEqual(reason, ExitReason.MIN_PROFIT_TP_HIT)
        self.assertEqual(p, exact_tp)
        self.assertEqual(t, 1.0)

        # Candle opens below SL
        c_sl_open = Candle(
            open_time_ms=1000,
            open=9.994,
            high=10.010,
            low=9.990,
            close=10.000,
            volume=100,
            close_time_ms=60000
        )
        p, reason, t = self.engine._resolve_candle_exit_order(
            c=c_sl_open,
            direction=OrderDirection.LONG,
            entry_price=entry_price,
            exact_tp=exact_tp,
            exact_sl=exact_sl,
            pu=pu,
            ps=ps,
            apply_slip=False,
            slippage_ticks=0,
            initial_sl=exact_sl
        )
        self.assertEqual(reason, ExitReason.STOP_LOSS_HIT)
        self.assertEqual(p, exact_sl)
        self.assertEqual(t, 1.0)

    def test_sub_candle_tp_first_long(self):
        # Long position: entry = 10.0, TP = 10.005, SL = 9.995
        entry_price = 10.0
        exact_tp = 10.005
        exact_sl = 9.995
        pu = 0.001
        ps = 3

        # 5m candle spanning 0 to 300,000 ms with high=10.010 and low=9.990 (both hit!)
        c_5m = Candle(
            open_time_ms=0,
            open=10.000,
            high=10.010,
            low=9.990,
            close=10.002,
            volume=500,
            close_time_ms=299999
        )

        # 1m sub-candles:
        # Min 1: price rises to 10.006 (hits TP!), low is 9.998 (above SL)
        # Min 2: price stays between 10.001 and 10.004
        # Min 3: price plunges to 9.990 (hits SL!)
        sub_1m = [
            Candle(open_time_ms=0, open=10.000, high=10.006, low=9.998, close=10.004, volume=100, close_time_ms=59999),
            Candle(open_time_ms=60000, open=10.004, high=10.005, low=10.001, close=10.002, volume=100, close_time_ms=119999),
            Candle(open_time_ms=120000, open=10.002, high=10.003, low=9.990, close=9.992, volume=100, close_time_ms=179999),
        ]
        self.engine.sub_candles_1m = sub_1m
        self.engine._sub_1m_timestamps = [c.open_time_ms for c in sub_1m]

        p, reason, t = self.engine._resolve_candle_exit_order(
            c=c_5m,
            direction=OrderDirection.LONG,
            entry_price=entry_price,
            exact_tp=exact_tp,
            exact_sl=exact_sl,
            pu=pu,
            ps=ps,
            apply_slip=False,
            slippage_ticks=0,
            initial_sl=exact_sl
        )
        # Sub-candle #1 reached TP first!
        self.assertEqual(reason, ExitReason.MIN_PROFIT_TP_HIT)
        self.assertEqual(p, exact_tp)
        self.assertAlmostEqual(t, 59.999, places=2)

    def test_sub_candle_sl_first_long(self):
        # Long position: entry = 10.0, TP = 10.005, SL = 9.995
        entry_price = 10.0
        exact_tp = 10.005
        exact_sl = 9.995
        pu = 0.001
        ps = 3

        # 5m candle spanning 0 to 300,000 ms with high=10.010 and low=9.990 (both hit!)
        c_5m = Candle(
            open_time_ms=0,
            open=10.000,
            high=10.010,
            low=9.990,
            close=10.002,
            volume=500,
            close_time_ms=299999
        )

        # 1m sub-candles:
        # Min 1: price drops to 9.992 (hits SL!), high is 10.002 (below TP)
        # Min 2: price rebounds to 10.008 (hits TP!)
        sub_1m = [
            Candle(open_time_ms=0, open=10.000, high=10.002, low=9.992, close=9.994, volume=100, close_time_ms=59999),
            Candle(open_time_ms=60000, open=9.994, high=10.008, low=9.993, close=10.005, volume=100, close_time_ms=119999),
        ]
        self.engine.sub_candles_1m = sub_1m
        self.engine._sub_1m_timestamps = [c.open_time_ms for c in sub_1m]

        p, reason, t = self.engine._resolve_candle_exit_order(
            c=c_5m,
            direction=OrderDirection.LONG,
            entry_price=entry_price,
            exact_tp=exact_tp,
            exact_sl=exact_sl,
            pu=pu,
            ps=ps,
            apply_slip=False,
            slippage_ticks=0,
            initial_sl=exact_sl
        )
        # Sub-candle #1 reached SL first!
        self.assertEqual(reason, ExitReason.STOP_LOSS_HIT)
        self.assertEqual(p, exact_sl)
        self.assertAlmostEqual(t, 59.999, places=2)

    def test_persisting_discrepancy_declares_sl(self):
        # Long position: entry = 10.0, TP = 10.005, SL = 9.995
        entry_price = 10.0
        exact_tp = 10.005
        exact_sl = 9.995
        pu = 0.001
        ps = 3

        # 5m candle
        c_5m = Candle(
            open_time_ms=0,
            open=10.000,
            high=10.010,
            low=9.990,
            close=10.000,
            volume=500,
            close_time_ms=299999
        )

        # Sub-candle #1 in the 5m candle ALSO hit both high 10.010 and low 9.990 with open at 10.000!
        sub_1m = [
            Candle(open_time_ms=0, open=10.000, high=10.010, low=9.990, close=10.000, volume=100, close_time_ms=59999),
        ]
        self.engine.sub_candles_1m = sub_1m
        self.engine._sub_1m_timestamps = [c.open_time_ms for c in sub_1m]

        p, reason, t = self.engine._resolve_candle_exit_order(
            c=c_5m,
            direction=OrderDirection.LONG,
            entry_price=entry_price,
            exact_tp=exact_tp,
            exact_sl=exact_sl,
            pu=pu,
            ps=ps,
            apply_slip=False,
            slippage_ticks=0,
            initial_sl=exact_sl
        )
        # Discrepancy persists -> declare SL!
        self.assertEqual(reason, ExitReason.STOP_LOSS_HIT)
        self.assertEqual(p, exact_sl)

    def test_1m_candle_discrepancy_declares_sl(self):
        # In a 1m backtest (no sub-candles below 1m)
        entry_price = 10.0
        exact_tp = 10.005
        exact_sl = 9.995
        pu = 0.001
        ps = 3

        c_1m = Candle(
            open_time_ms=0,
            open=10.000,
            high=10.010,
            low=9.990,
            close=10.002,
            volume=100,
            close_time_ms=59999
        )
        self.engine.sub_candles_1m = []
        self.engine._sub_1m_timestamps = []

        p, reason, t = self.engine._resolve_candle_exit_order(
            c=c_1m,
            direction=OrderDirection.LONG,
            entry_price=entry_price,
            exact_tp=exact_tp,
            exact_sl=exact_sl,
            pu=pu,
            ps=ps,
            apply_slip=True,
            slippage_ticks=1,
            initial_sl=exact_sl
        )
        # Discrepancy on 1m candle -> declares SL with slippage!
        self.assertEqual(reason, ExitReason.STOP_LOSS_HIT)
        self.assertEqual(p, round(exact_sl - 0.001, 3)) # 9.994

    def test_short_sub_candle_disambiguation(self):
        # Short position: entry = 10.0, TP = 9.995, SL = 10.005
        entry_price = 10.0
        exact_tp = 9.995
        exact_sl = 10.005
        pu = 0.001
        ps = 3

        c_5m = Candle(
            open_time_ms=0,
            open=10.000,
            high=10.010,
            low=9.990,
            close=10.000,
            volume=500,
            close_time_ms=299999
        )

        # Sub-candles:
        # Min 1: drops to 9.993 (TP hit!), high is 10.002 (below SL)
        # Min 2: spikes to 10.008 (SL hit!)
        sub_1m = [
            Candle(open_time_ms=0, open=10.000, high=10.002, low=9.993, close=9.996, volume=100, close_time_ms=59999),
            Candle(open_time_ms=60000, open=9.996, high=10.008, low=9.995, close=10.004, volume=100, close_time_ms=119999),
        ]
        self.engine.sub_candles_1m = sub_1m
        self.engine._sub_1m_timestamps = [c.open_time_ms for c in sub_1m]

        p, reason, t = self.engine._resolve_candle_exit_order(
            c=c_5m,
            direction=OrderDirection.SHORT,
            entry_price=entry_price,
            exact_tp=exact_tp,
            exact_sl=exact_sl,
            pu=pu,
            ps=ps,
            apply_slip=False,
            slippage_ticks=0,
            initial_sl=exact_sl
        )
        self.assertEqual(reason, ExitReason.MIN_PROFIT_TP_HIT)
        self.assertEqual(p, exact_tp)


if __name__ == "__main__":
    unittest.main()
