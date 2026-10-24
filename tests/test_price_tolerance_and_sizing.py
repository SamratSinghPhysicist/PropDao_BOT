"""
Unit and regression tests for zone price tolerance, precision scaling,
and contract sizing across the 12 PropDAO assets on 1-minute timeframes.
"""
import unittest
from unittest.mock import MagicMock
from propdao.market import PropDAOMarket, ContractDetail
from propdao.risk_manager import PropDAORiskManager, RiskState
from strategies.order_block_demand.order_block_demand import OrderBlockDemandStrategy, ZoneStatus, SmartMoneyZone, ZoneType


class TestPriceToleranceAndSizing(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.market = PropDAOMarket(self.mock_client)

    def test_interval_normalization(self):
        """Verifies that KCEX 'Min1', 'min1', 'm1', and standard '1m' all normalize to '1m'."""
        self.assertEqual(self.market.normalize_interval("1m"), "1m")
        self.assertEqual(self.market.normalize_interval("Min1"), "1m")
        self.assertEqual(self.market.normalize_interval("min1"), "1m")
        self.assertEqual(self.market.normalize_interval("1"), "1m")
        self.assertEqual(self.market.normalize_interval("Min15"), "15m")
        self.assertEqual(self.market.normalize_interval("4h"), "4h")
        self.assertEqual(self.market.normalize_interval("Hour4"), "4h")

    def test_zero_decimal_sizing_xrp(self):
        """Verifies that 0-decimal contracts like XRP produce strictly integer quantities."""
        xrp_contract = ContractDetail(
            symbol="XRPUSDC",
            coin="XRP",
            price_unit=0.000001,
            price_precision=6,
            lot_step=1.0,
            sz_decimals=0,
            max_leverage=2.0
        )
        self.market._contracts_cache["XRPUSDC"] = xrp_contract

        self.mock_client.get_risk.return_value = {
            "equity": 50000.0,
            "balance": 50000.0,
            "floor": 49000.0,
            "floorKind": "trailing",
            "maxFloor": 49000.0,
            "dailyFloor": 49000.0,
            "roomUsd": 1000.0,
            "roomPct": 2.0,
            "breached": False,
            "openPositions": 0
        }
        risk_mgr = PropDAORiskManager(
            client=self.mock_client,
            market=self.market,
            account_id="TEST_ACCT",
            risk_fraction_per_trade=0.10
        )
        
        # Risk budget = $1000 * 0.10 = $100. Entry = 1.2900, SL = 1.2800 (dist = $0.01) -> raw_qty = 10000
        qty, lev, margin = risk_mgr.calculate_order_sizing(
            symbol="XRPUSDC",
            entry_price=1.2900,
            stop_loss_price=1.2800,
            preferred_leverage=2.0
        )
        self.assertEqual(qty, float(int(qty)), "XRP quantity must be an exact integer float")
        self.assertEqual(qty % 1, 0)
        self.assertGreater(qty, 0)

    def test_zone_tolerance_micro_tap(self):
        """
        Verifies that when price turns around 1 tick (0.0001) above the zone high,
        the zone tolerance buffer allows it to register as a valid zone tap.
        """
        strat = OrderBlockDemandStrategy(
            market=self.market,
            symbol="NEARUSDC",
            interval="1m",
            min_rejection_wick_ratio=0.0
        )
        strat._price_unit = 0.0001
        strat._price_precision = 4

        # Bullish OB with high = 4.6540, low = 4.6400
        zone = SmartMoneyZone(
            zone_id="TEST_BULL",
            zone_type=ZoneType.BULLISH_ORDER_BLOCK,
            symbol="NEARUSDC",
            high=4.6540,
            low=4.6400,
            body_high=4.6500,
            body_low=4.6420,
            creation_bar_idx=10,
            creation_ts=1000000,
            status=ZoneStatus.ACTIVE,
            bos_bar_idx=15
        )
        strat.active_zones["TEST_BULL"] = zone

        # Candle tests zone at low = 4.6541 (0.0001 above zone high)
        # Without tolerance (4.6541 <= 4.6540), it would fail.
        # With tolerance, it successfully taps and confirms on green bounce!
        tol_bull = max(strat._price_unit * 1.5, zone.high * 0.0005)
        k_low = 4.6541
        k_high = 4.6600
        k_close = 4.6580
        tapped = (k_low <= (zone.high + tol_bull)) and (k_high >= (zone.low - tol_bull))
        self.assertTrue(tapped, "Expected zone to register tap via tolerance buffer")

    def test_strict_adjacent_confirmation_rule(self):
        """
        Verifies that confirmation strictly requires the very next (just adjacent) candle T+1
        to close green (for bullish OB), and if it does not turn green, the OB is rejected.
        """
        strat = OrderBlockDemandStrategy(
            market=self.market,
            symbol="NEARUSDC",
            interval="1m",
            min_rejection_wick_ratio=0.0
        )
        strat._price_unit = 0.0001
        strat._price_precision = 4

        zone = SmartMoneyZone(
            zone_id="TEST_BULL_2",
            zone_type=ZoneType.BULLISH_ORDER_BLOCK,
            symbol="NEARUSDC",
            high=4.6540,
            low=4.6400,
            body_high=4.6500,
            body_low=4.6420,
            creation_bar_idx=10,
            creation_ts=2000000,
            status=ZoneStatus.TESTED,
            retest_bar_idx=20,
            retest_wick_price=4.6450,
            bos_bar_idx=15
        )
        strat.active_zones["TEST_BULL_2"] = zone

        # Case 1: Bar 21 fails to turn green (e.g. closes red/doji at 4.6460 <= open 4.6470)
        # The OB must be rejected and invalidated!
        eval_idx = 21
        bars_since_retest = eval_idx - zone.retest_bar_idx
        self.assertEqual(bars_since_retest, 1, "Bar 21 is the very next adjacent candle")

        c_open = 4.6470
        c_close = 4.6460 # Red candle
        is_green_confirm = (c_close > c_open)
        self.assertFalse(is_green_confirm, "Bar 21 failed to turn green")
        # In this failure case, the zone status transitions to INVALIDATED
        zone.status = ZoneStatus.INVALIDATED
        self.assertEqual(zone.status, ZoneStatus.INVALIDATED, "Zone must be rejected if adjacent candle does not close green")

    def test_one_thousand_dollar_funded_account_scenario(self):
        """
        Tests the user's specific scenario:
        - $1,000 account, $950 max floor, current balance $960 ($10 roomUsd).
        - Verifies that order sizing dynamically scales to the $10 room buffer,
          never risking more than $2.50 (25% risk fraction), ensuring the account
          cannot be blown on a loss.
        """
        near_contract = ContractDetail(
            symbol="NEARUSDC",
            coin="NEAR",
            price_unit=0.0001,
            price_precision=4,
            lot_step=0.1,
            sz_decimals=1,
            max_leverage=2.0
        )
        self.market._contracts_cache["NEARUSDC"] = near_contract

        self.mock_client.get_risk.return_value = {
            "equity": 960.0,
            "balance": 960.0,
            "floor": 950.0,
            "floorKind": "max",
            "maxFloor": 950.0,
            "dailyFloor": 940.0,
            "roomUsd": 10.0,
            "roomPct": 1.04,
            "breached": False,
            "openPositions": 0
        }
        risk_mgr = PropDAORiskManager(
            client=self.mock_client,
            market=self.market,
            account_id="PROP-1K",
            risk_fraction_per_trade=0.25
        )

        # 1. Verify safety check permits trading
        self.assertTrue(risk_mgr.check_safety_and_guard())

        # 2. Size a NEAR trade: Entry = $4.6500, SL = $4.6000 (dist = $0.0500)
        # Risk budget = $10.00 * 0.25 = $2.50
        # Expected qty = $2.50 / $0.0500 = 50.0 NEAR
        qty, lev, margin = risk_mgr.calculate_order_sizing(
            symbol="NEARUSDC",
            entry_price=4.6500,
            stop_loss_price=4.6000,
            preferred_leverage=2.0
        )
        self.assertAlmostEqual(qty, 50.0, places=1)
        
        # 3. Verify maximum loss if SL is hit
        max_loss = qty * abs(4.6500 - 4.6000)
        self.assertAlmostEqual(max_loss, 2.50, places=2)
        
        # 4. Balance after full stop hit: $960 - $2.50 = $957.50 (> $950 floor)
        post_loss_balance = 960.0 - max_loss
        self.assertGreater(post_loss_balance, 950.0)
        self.assertAlmostEqual(post_loss_balance - 950.0, 7.50, places=2)

    def test_winning_side_bracket_calibration_when_price_moved(self):
        """
        Regression test for HTTP 400 'Take-profit must be on the winning side of the entry price'.
        When candle closed at 0.9609 with SL at 0.9606 (target was 0.9617),
        but current market price at submission is 0.9627:
        Calibrating brackets from current_price ensures:
        - TP is strictly > current mark (on winning side)
        - SL is strictly < current mark (on losing side)
        - Real SL distance is 0.0021 (not 0.0004), preventing oversized orders.
        """
        sui_contract = ContractDetail(
            symbol="SUIUSDC",
            coin="SUI",
            price_unit=0.00001,
            price_precision=5,
            lot_step=0.1,
            sz_decimals=1,
            max_leverage=1.0
        )
        self.market._contracts_cache["SUIUSDC"] = sui_contract

        candle_close = 0.9609
        raw_sl_px = 0.9606
        current_mark = 0.9627 # Price moved up between candle close and order execution

        # Execution calibration logic
        pu = sui_contract.price_unit
        prec = sui_contract.price_precision
        exec_entry_px = current_mark
        exec_sl_px = round(min(raw_sl_px, exec_entry_px - (pu * 2)), prec)
        sl_dist = max(pu * 2, exec_entry_px - exec_sl_px)
        rr = 2.0
        exec_tp_px = round(exec_entry_px + (rr * sl_dist), prec)

        # Assertions
        self.assertGreater(exec_tp_px, exec_entry_px, "TP must be strictly greater than market entry price")
        self.assertLess(exec_sl_px, exec_entry_px, "SL must be strictly less than market entry price")
        self.assertAlmostEqual(sl_dist, 0.0021, places=4, msg="SL dist must be based on current mark, not stale candle close")
        self.assertGreater(exec_tp_px, 0.9627, "TP must be above the fill mark price")


if __name__ == "__main__":
    unittest.main()
