"""
Unit tests for PropDAO Execution Engine, Paper Engine, and Risk Management.
"""

import unittest
from propdao.models import OrderSide, OrderDirection, ContractDetail
from propdao.paper_engine import PropDAOPaperEngine
from propdao.risk_manager import PropDAORiskManager
from propdao.order_manager import PropDAOOrderManager
from propdao.market import PropDAOMarket
from engine.position_tracker import PositionTracker


class TestExecutionEngine(unittest.TestCase):
    def setUp(self):
        self.paper = PropDAOPaperEngine(
            starting_balance=25000.0,
            max_drawdown_pct=5.0,   # Floor at $23,750
            daily_drawdown_pct=2.0, # Floor at $24,500
            account_id="test-prop-01"
        )
        self.market = PropDAOMarket(client=self.paper)
        self.risk_mgr = PropDAORiskManager(
            client=self.paper,
            market=self.market,
            account_id="test-prop-01",
            risk_fraction_per_trade=0.25
        )
        self.order_mgr = PropDAOOrderManager(client=self.paper, account_id="test-prop-01")

    def test_risk_budget_and_sizing(self):
        # Initial balance 25k, daily floor 2% ($24,500), roomUsd = $500
        risk = self.risk_mgr.get_risk_state()
        self.assertEqual(risk.balance, 25000.0)
        self.assertEqual(risk.daily_floor, 24500.0)
        self.assertEqual(risk.room_usd, 500.0)

        # Budget = 25% of $500 = $125
        # If entry is 50,000 and SL is 49,000 (distance 1,000), qty = 125 / 1000 = 0.125
        qty, lev, margin = self.risk_mgr.calculate_order_sizing(
            symbol="BTCUSDC",
            entry_price=50000.0,
            stop_loss_price=49000.0,
            preferred_leverage=2.0
        )
        self.assertAlmostEqual(qty, 0.125, places=3)
        self.assertEqual(lev, 2.0)
        # Margin = (0.125 * 50000) / 2 = 3125.0
        self.assertAlmostEqual(margin, 3125.0, places=1)

    def test_paper_market_order_and_sl_trigger(self):
        self.paper.update_mark_price("BTCUSDC", 50000.0)
        res = self.order_mgr.open_market(
            symbol="BTCUSDC",
            side=OrderSide.BUY,
            qty=0.1,
            leverage=2.0,
            sl_price=49000.0,
            tp_price=52000.0
        )
        positions = self.order_mgr.refresh_positions()
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.entry, 50000.0)
        self.assertEqual(pos.sl_price, 49000.0)

        # Price drops to 48900 -> Stop Loss should trigger automatically
        self.paper.update_mark_price("BTCUSDC", 48900.0)
        positions = self.order_mgr.refresh_positions()
        self.assertEqual(len(positions), 0)

        trades = self.paper.get_trades("test-prop-01")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "Stop Loss")
        self.assertAlmostEqual(trades[0]["exit"], 49000.0)

    def test_partial_tp_and_breakeven_lock(self):
        tracker = PositionTracker(symbol="BTCUSDC")
        self.paper.update_mark_price("BTCUSDC", 50000.0)

        self.order_mgr.open_market(
            symbol="BTCUSDC",
            side=OrderSide.BUY,
            qty=0.2,
            leverage=2.0,
            sl_price=49000.0,
            tp_price=52000.0
        )
        pos = self.order_mgr.refresh_positions()[0]
        tracker.register_entry(
            position=pos,
            target_1to1_price=51000.0,
            target_1to2_price=52000.0,
            sl_price=49000.0
        )

        # 1:1 TP hit at 51,000
        self.paper.update_mark_price("BTCUSDC", 51000.0)
        self.order_mgr.close_position_market(pos.id, percent=0.5)
        tracker.register_partial_exit(closed_qty=0.1, exit_price=51000.0, realized_pnl=100.0, fee=2.29)
        self.order_mgr.move_sl_to_breakeven(pos, buffer_ticks=1, tick_size=0.1)

        # Verify position remaining qty is 0.1 and stop loss moved to breakeven
        updated_pos = self.order_mgr.refresh_positions()[0]
        self.assertAlmostEqual(updated_pos.qty, 0.1, places=3)
        self.assertGreaterEqual(updated_pos.sl_price, 50000.0)

    def test_drawdown_floor_breach(self):
        # Initial balance 25k, max floor $23,750 (5% static)
        # Open 1.0 BTC @ 50,000, 2x leverage
        self.paper.update_mark_price("BTCUSDC", 50000.0)
        self.order_mgr.open_market(symbol="BTCUSDC", side=OrderSide.BUY, qty=1.0, leverage=2.0)

        # Price drops to 48,000 -> Unrealized loss $2,000 -> Equity = 23,000 <= 23,750 floor!
        self.paper.update_mark_price("BTCUSDC", 48000.0)

        risk = self.risk_mgr.get_risk_state()
        self.assertTrue(risk.breached)
        self.assertEqual(len(self.order_mgr.refresh_positions()), 0)


if __name__ == "__main__":
    unittest.main()
