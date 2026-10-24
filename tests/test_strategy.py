"""
Unit tests for OrderBlockDemandStrategy logic in PropDAO.
Verifies that Vivek Yadav's Smart Money Concepts (SMC) rules remain 100% intact.
"""

import unittest
from propdao.models import OrderDirection, ContractDetail
from strategies.order_block_demand.order_block_demand import (
    OrderBlockDemandStrategy,
    ZoneType,
    ZoneStatus,
    SmartMoneyZone,
    SwingStructureDetector
)


class MockMarket:
    def __init__(self):
        self.contract = ContractDetail(
            symbol="BTCUSDC",
            coin="BTC",
            price_unit=0.1,
            price_precision=1,
            lot_step=0.00001,
            max_leverage=2.0
        )

    def get_contract_detail(self, symbol: str) -> ContractDetail:
        return self.contract

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 120):
        return []


class TestOrderBlockDemandStrategy(unittest.TestCase):
    def setUp(self):
        self.market = MockMarket()
        self.strategy = OrderBlockDemandStrategy(
            market=self.market,
            symbol="BTCUSDC",
            interval="15m",
            pivot_len=3,
            risk_reward_ratio=2.0,
            partial_tp_enabled=True
        )

    def test_swing_pivot_detection(self):
        # Peak at index 3
        highs = [100.0, 102.0, 104.0, 110.0, 105.0, 103.0, 101.0]
        lows = [98.0, 99.0, 100.0, 105.0, 101.0, 100.0, 98.0]
        timestamps = [1000 * i for i in range(len(highs))]

        swings = SwingStructureDetector.find_swings(highs, lows, timestamps, left_bars=3, right_bars=3)
        self.assertTrue(any(s.is_high and s.price == 110.0 for s in swings))

    def test_indicator_calculation_bullish_bos_and_retest(self):
        # Construct synthetic candle series:
        # 1. Swing High around bar 3 (price 105)
        # 2. Pullback to bar 6 (red candle, origin low 95)
        # 3. Piercing green candle breaking 105 (BOS) at bar 8
        # 4. Pullback tap into zone [95-98] at bar 10 with lower rejection wick
        # 5. Confirmation green candle on the very next bar 11 (close 101 > open 97)
        timestamps = [i * 900000 for i in range(15)]
        opens = [100.0, 101.0, 103.0, 105.0, 103.0, 100.0, 98.0, 102.0, 106.0, 104.0, 96.0, 97.0, 100.0, 102.0, 105.0]
        highs = [102.0, 103.0, 105.0, 106.0, 104.0, 101.0, 99.0, 104.0, 108.0, 105.0, 99.0, 102.0, 103.0, 105.0, 107.0]
        lows =  [99.0,  100.0, 102.0, 103.0, 101.0, 97.0,  95.0, 100.0, 103.0, 102.0, 95.5, 96.0, 98.0,  101.0, 103.0]
        closes =[101.0, 103.0, 104.0, 104.0, 101.0, 98.0,  96.0, 103.0, 107.0, 103.0, 98.0, 101.0, 102.0, 104.0, 106.0]

        zones, trades = self.strategy.calc_indicator_zones_and_trades(
            timestamps, opens, highs, lows, closes, pivot_len=2
        )

        self.assertIsInstance(zones, list)
        self.assertIsInstance(trades, list)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["start_idx"], 11, "Trade must be entered on the adjacent confirmation candle (bar 11), not bar 10")
        self.assertEqual(trades[0]["type"], "long")
        self.assertEqual(trades[0]["entry"], 101.0)

    def test_bullish_ob_rejected_when_confirmation_fails(self):
        # Same setup, but bar 11 fails to close green (closes red: open 97.0, close 96.5)
        timestamps = [i * 900000 for i in range(15)]
        opens = [100.0, 101.0, 103.0, 105.0, 103.0, 100.0, 98.0, 102.0, 106.0, 104.0, 96.0, 97.0, 100.0, 102.0, 105.0]
        highs = [102.0, 103.0, 105.0, 106.0, 104.0, 101.0, 99.0, 104.0, 108.0, 105.0, 99.0, 98.0, 103.0, 105.0, 107.0]
        lows =  [99.0,  100.0, 102.0, 103.0, 101.0, 97.0,  95.0, 100.0, 103.0, 102.0, 95.5, 96.0, 98.0,  101.0, 103.0]
        closes =[101.0, 103.0, 104.0, 104.0, 101.0, 98.0,  96.0, 103.0, 107.0, 103.0, 98.0, 96.5, 102.0, 104.0, 106.0]

        zones, trades = self.strategy.calc_indicator_zones_and_trades(
            timestamps, opens, highs, lows, closes, pivot_len=2
        )

        self.assertEqual(len(trades), 0, "No trade should be taken when adjacent candle does not close green")
        bull_zones = [z for z in zones if z.is_bullish]
        self.assertTrue(any(z.status == ZoneStatus.INVALIDATED for z in bull_zones), "OB must be rejected/invalidated")

    def test_indicator_calculation_bearish_bos_and_retest(self):
        # Bearish setup:
        # Swing Low around bar 3
        # Pullback to green candle origin high around bar 6
        # Piercing breakdown below swing low around bar 8
        # Retest tap into bearish OB around bar 10 with upper rejection wick
        # Red confirmation candle on the very next bar 11 (close < open)
        timestamps = [i * 900000 for i in range(15)]
        opens = [100.0, 99.0, 97.0, 95.0, 97.0, 100.0, 102.0, 98.0, 94.0, 96.0, 104.0, 103.0, 100.0, 98.0, 95.0]
        highs = [101.0, 100.0, 98.0, 96.0, 99.0, 103.0, 105.0, 100.0, 95.0, 97.0, 104.5, 104.0, 102.0, 99.0, 97.0]
        lows =  [98.0,  97.0, 95.0, 94.0, 96.0, 99.0,  101.0, 96.0, 92.0, 95.0, 101.0, 98.0,  98.0,  96.0, 93.0]
        closes =[99.0,  97.0, 96.0, 96.0, 99.0, 102.0, 104.0, 97.0, 93.0, 97.0, 102.0, 99.0,  99.0,  96.0, 94.0]

        zones, trades = self.strategy.calc_indicator_zones_and_trades(
            timestamps, opens, highs, lows, closes, pivot_len=2
        )

        self.assertIsInstance(zones, list)
        self.assertIsInstance(trades, list)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["start_idx"], 11, "Trade must be entered on the adjacent confirmation candle (bar 11), not bar 10")
        self.assertEqual(trades[0]["type"], "short")
        self.assertEqual(trades[0]["entry"], 99.0)

    def test_bearish_ob_rejected_when_confirmation_fails(self):
        # Bearish setup where bar 11 fails to close red (closes green)
        timestamps = [i * 900000 for i in range(15)]
        opens = [100.0, 99.0, 97.0, 95.0, 97.0, 100.0, 102.0, 98.0, 94.0, 96.0, 104.0, 99.0, 100.0, 98.0, 95.0]
        highs = [101.0, 100.0, 98.0, 96.0, 99.0, 103.0, 105.0, 100.0, 95.0, 97.0, 104.5, 104.0, 102.0, 99.0, 97.0]
        lows =  [98.0,  97.0, 95.0, 94.0, 96.0, 99.0,  101.0, 96.0, 92.0, 95.0, 101.0, 98.0,  98.0,  96.0, 93.0]
        closes =[99.0,  97.0, 96.0, 96.0, 99.0, 102.0, 104.0, 97.0, 93.0, 97.0, 102.0, 103.5, 99.0,  96.0, 94.0]

        zones, trades = self.strategy.calc_indicator_zones_and_trades(
            timestamps, opens, highs, lows, closes, pivot_len=2
        )

        self.assertEqual(len(trades), 0, "No trade should be taken when adjacent candle does not close red")
        bear_zones = [z for z in zones if z.is_bearish]
        self.assertTrue(any(z.status == ZoneStatus.INVALIDATED for z in bear_zones), "Bearish OB must be rejected/invalidated")

    def test_zone_invalidation_rule(self):
        # A bullish zone invalidated when price closes below its bottom
        zone = SmartMoneyZone(
            zone_id="OB_BULL_1",
            zone_type=ZoneType.BULLISH_ORDER_BLOCK,
            symbol="BTCUSDC",
            high=100.0,
            low=95.0,
            status=ZoneStatus.ACTIVE
        )
        self.strategy.active_zones[zone.zone_id] = zone

        # Candle closes at 94.0 (below 95.0) -> must invalidate
        self.strategy.update_zone_lifecycle(
            new_zones=[],
            current_bar_idx=0,
            opens=[96.0],
            highs=[97.0],
            lows=[93.0],
            closes=[94.0]
        )

        self.assertNotIn("OB_BULL_1", self.strategy.active_zones)
        self.assertEqual(zone.status, ZoneStatus.INVALIDATED)


if __name__ == "__main__":
    unittest.main()
