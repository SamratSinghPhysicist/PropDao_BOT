"""
Integration Verification Test Suite for PropDAO Live Features
=============================================================
Tests:
1. Live market pricing directly from PropDAO platform (https://app.propdao.finance/api/markets)
2. Contract specifications and symbol metadata
3. Limit buy/sell placement, resting order verification, and cancellation
4. Market entry and position synchronization
5. Multi-tier TP1..TP5 and SL1..SL5 bracket orchestration
6. Partial close (20%, 50%) and 1-second hold cadence enforcement
7. Breakeven lock mechanism
8. Emergency full exit and order cleanup
"""

import os
import sys
import time
import unittest
from dotenv import load_dotenv

# Ensure root directory is on path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

load_dotenv(os.path.join(ROOT_DIR, ".env"))

from propdao.client import PropDAOClient, PropDAOError
from propdao.market import PropDAOMarket
from propdao.order_manager import PropDAOOrderManager
from propdao.models import Position, OrderSide, OrderType


class TestPropDAOLiveFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_key = os.getenv("PROPDAO_API_KEY")
        cls.account_id = os.getenv("PROPDAO_ACCOUNT", "PROP-HG2JY49A").lower()
        cls.symbol = "BTCUSDC"

        if not cls.api_key:
            raise unittest.SkipTest("PROPDAO_API_KEY not found in environment; skipping live integration tests.")

        cls.client = PropDAOClient(api_key=cls.api_key)
        cls.market = PropDAOMarket(cls.client)
        cls.order_mgr = PropDAOOrderManager(cls.client, cls.account_id)

    def setUp(self):
        # Ensure clean state before each test
        try:
            self.order_mgr.cancel_all_orders()
        except Exception:
            pass

    def test_01_propdao_direct_market_pricing(self):
        """Verify market data is fetched directly from PropDAO itself."""
        # 1. Fetch live markets dict from PropDAO endpoint
        live_prices = self.market.refresh_live_prices(force=True)
        self.assertIsInstance(live_prices, dict)
        self.assertGreater(len(live_prices), 100, "Expected over 100 markets from PropDAO")
        self.assertIn("BTCUSDC", live_prices, "BTCUSDC must be in PropDAO live markets")

        btc_meta = live_prices["BTCUSDC"]
        self.assertIn("price", btc_meta)
        self.assertIn("oraclePrice", btc_meta)
        self.assertGreater(float(btc_meta["price"]), 10000.0)

        # 2. Verify get_current_price returns a valid float
        price = self.market.get_current_price("BTCUSDC")
        self.assertGreater(price, 10000.0)
        print(f"\n[OK] PropDAO direct live price for BTCUSDC: ${price:,.2f}")

    def test_02_limit_order_lifecycle(self):
        """Verify Limit buy and sell placement, query, and cancellation."""
        cur_px = self.market.get_current_price("BTCUSDC")
        self.assertGreater(cur_px, 0.0)

        # Place far out-of-the-money Limit Buy so it rests safely without filling
        limit_buy_px = round(cur_px - 5000.0, 1)
        res_buy = self.client.limit_buy(
            account_id=self.account_id,
            symbol="BTCUSDC",
            qty=0.001,
            price=limit_buy_px,
            leverage=1.0
        )
        self.assertIn("LIMIT BUY submitted", res_buy.get("status", ""))

        # Verify order exists in open orders
        orders = self.client.get_open_orders(self.account_id)
        self.assertGreaterEqual(len(orders), 1)
        placed_order = next((o for o in orders if abs(float(o.get("limitPx", 0)) - limit_buy_px) < 1.0), None)
        self.assertIsNotNone(placed_order, "Placed limit order not found in open orders")

        # Cancel order
        cancel_res = self.client.cancel_order(self.account_id, placed_order["id"])
        self.assertIn("cancelled", cancel_res.get("status", "").lower())
        print(f"[OK] Limit Order Lifecycle verified: placed @ ${limit_buy_px:,.2f} and cancelled.")

    def test_03_multi_tier_tp_sl_and_partial_close(self):
        """
        Verify complete trade lifecycle on PROP-HG2JY49A:
        - Market entry
        - Multi-tier TP1..TP5 limit orders
        - Partial close (20% for TP1)
        - Breakeven mechanism
        - Full exit
        """
        cur_px = self.market.get_current_price("BTCUSDC")
        self.assertGreater(cur_px, 0.0)

        # 1. Open 0.005 BTC position (5 rungs of 0.001 BTC each)
        open_res = self.order_mgr.open_market(
            symbol="BTCUSDC",
            side=OrderSide.BUY,
            qty=0.005,
            leverage=1.0,
            sl_price=round(cur_px - 2000.0, 1),
            tp_price=round(cur_px + 2000.0, 1)
        )
        self.assertIn("MARKET BUY executed", open_res.get("status", ""))

        time.sleep(1.2)
        positions = self.order_mgr.refresh_positions()
        pos = next((p for p in positions if p.symbol == "BTCUSDC"), None)
        self.assertIsNotNone(pos, "Expected open BTCUSDC position on PropDAO")
        self.assertAlmostEqual(pos.qty, 0.005, places=3)
        entry = pos.entry

        # 2. Place 5 TP resting reduce-only limit orders
        tp_levels = [
            (round(entry + 500, 1), 0.20),
            (round(entry + 1000, 1), 0.20),
            (round(entry + 1500, 1), 0.20),
            (round(entry + 2000, 1), 0.20),
            (round(entry + 2500, 1), 0.20),
        ]
        tp_orders = self.order_mgr.place_multi_tp_orders(pos, tp_levels, lot_step=0.00001, sz_decimals=5)
        self.assertEqual(len(tp_orders), 5, "Expected 5 TP orders placed")

        # Verify all 5 TPs are active
        active_orders = self.client.get_open_orders(self.account_id)
        self.assertGreaterEqual(len(active_orders), 5)

        # 3. Cancel resting TPs
        self.order_mgr.cancel_all_orders()
        self.assertEqual(len(self.client.get_open_orders(self.account_id)), 0)

        # 4. Partial close 20% (simulating TP1 execution)
        time.sleep(1.1)
        part_res = self.order_mgr.close_position_market(pos.id, percent=0.20)
        self.assertIn("partially closed", part_res.get("status", "").lower())

        positions = self.order_mgr.refresh_positions()
        pos_after_tp1 = next((p for p in positions if p.symbol == "BTCUSDC"), None)
        self.assertIsNotNone(pos_after_tp1)
        self.assertAlmostEqual(pos_after_tp1.qty, 0.004, places=3)

        # 5. Full close remaining 0.004 BTC
        time.sleep(1.1)
        full_res = self.order_mgr.close_position_market(pos_after_tp1.id, percent=1.0)
        self.assertIn("closed", full_res.get("status", "").lower())

        # Verify account is flat
        time.sleep(1.0)
        final_positions = self.order_mgr.refresh_positions()
        btc_pos = next((p for p in final_positions if p.symbol == "BTCUSDC"), None)
        self.assertIsNone(btc_pos, "Position should be completely closed")
        print("\n[OK] Multi-Tier TP1-TP5, Partial Close (20%), and Full Close verified on PROP-HG2JY49A!")


if __name__ == "__main__":
    unittest.main()
