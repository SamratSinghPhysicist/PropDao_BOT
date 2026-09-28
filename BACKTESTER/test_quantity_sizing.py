"""
Unit tests for Quantity Sizing & Margin-Based Sizing (Local & Cloud Backtesting + Live Execution).
Verifies:
1. MARGIN_PCT calculation: Margin % of available balance * Leverage = Position Size.
2. FIXED_MARGIN calculation: Fixed USDT Margin * Leverage = Position Size.
3. CONTRACTS and MULTIPLIER resolution across different pairs (TRUMP, DOGE, BTC, ETH, SOL).
4. Contract Spec repository coverage & dynamic offline fallback in BacktestMarket.
5. Sizing clamps when requested margin exceeds wallet balance.
"""

import unittest
import os
import sys

# Ensure ROOT_DIR is in sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.config import BacktestConfig
from BACKTESTER.engine.market_sim import DEFAULT_CONTRACTS, BacktestMarket
from BACKTESTER.engine.execution_sim import BacktestExecutionEngine


class TestQuantitySizing(unittest.TestCase):

    def setUp(self):
        self.market = BacktestMarket()

    def test_contract_spec_repository(self):
        """Verify expanded contract specifications for major pairs."""
        btc = self.market.get_contract_detail("BTC_USDT")
        self.assertEqual(btc.base_coin, "BTC")
        self.assertEqual(btc.contract_size, 0.0001)

        eth = self.market.get_contract_detail("ETH_USDT")
        self.assertEqual(eth.base_coin, "ETH")
        self.assertEqual(eth.contract_size, 0.001)

        sol = self.market.get_contract_detail("SOL_USDT")
        self.assertEqual(sol.base_coin, "SOL")
        self.assertEqual(sol.contract_size, 0.01)

        doge = self.market.get_contract_detail("DOGE_USDT")
        self.assertEqual(doge.contract_size, 10.0)

        trump = self.market.get_contract_detail("TRUMP_USDT")
        self.assertEqual(trump.contract_size, 0.1)

        pepe = self.market.get_contract_detail("PEPE_USDT")
        self.assertEqual(pepe.contract_size, 100000.0)

    def test_dynamic_contract_estimation_fallback(self):
        """Verify dynamic offline estimation creates realistic contract sizes for unknown pairs."""
        # High priced asset ($50,000)
        m_high = BacktestMarket()
        m_high.current_price = 50000.0
        high_priced = m_high.get_contract_detail("UNKNOWN_HIGH_USDT")
        self.assertLess(high_priced.contract_size, 1.0)
        # 1 contract value should be ~$10 - $50
        contract_notional = high_priced.contract_size * 50000.0
        self.assertTrue(1.0 <= contract_notional <= 100.0)

        # Low priced meme asset ($0.00002)
        m_low = BacktestMarket()
        m_low.current_price = 0.00002
        low_priced = m_low.get_contract_detail("UNKNOWN_LOW_USDT")
        self.assertGreater(low_priced.contract_size, 1000.0)
        low_notional = low_priced.contract_size * 0.00002
        self.assertTrue(1.0 <= low_notional <= 100.0)

    def test_margin_pct_sizing_math(self):
        """
        Verify MARGIN_PCT volume sizing math:
        Target Margin = Balance * (margin_pct / 100)
        Target Notional = Target Margin * Leverage
        Contracts = Target Notional / (contract_size * price)
        """
        cfg = BacktestConfig(
            symbol="BTC_USDT",
            volume_mode="MARGIN_PCT",
            margin_pct=10.0,
            leverage=50,
            initial_balance_usdt=100.0,
            timeframe="1m"
        )
        engine = BacktestExecutionEngine(config=cfg)
        entry_price = 50000.0  # BTC at $50,000, cs = 0.0001 -> 1 contract = $5.0
        contracts = engine._resolve_simulated_contracts(entry_price)

        # 10% of $100 = $10.0 margin
        # $10.0 margin * 50 leverage = $500 target notional
        # 1 contract notional = 0.0001 * 50000 = $5.00
        # $500 / $5.00 = 100 contracts
        self.assertEqual(contracts, 100)

        # Committed margin should be exactly 100 * 5.0 / 50 = $10.0
        committed_margin = (contracts * 0.0001 * entry_price) / cfg.leverage
        self.assertAlmostEqual(committed_margin, 10.0, places=2)

    def test_fixed_margin_sizing_math(self):
        """
        Verify FIXED_MARGIN volume sizing math:
        Target Notional = fixed_margin_usdt * Leverage
        Contracts = Target Notional / (contract_size * price)
        """
        cfg = BacktestConfig(
            symbol="DOGE_USDT",
            volume_mode="FIXED_MARGIN",
            fixed_margin_usdt=5.0,
            leverage=50,
            initial_balance_usdt=100.0,
            timeframe="1m"
        )
        engine = BacktestExecutionEngine(config=cfg)
        entry_price = 0.20  # DOGE at $0.20, contract_size = 10.0 -> 1 contract = $2.0
        contracts = engine._resolve_simulated_contracts(entry_price)

        # $5.0 margin * 50 leverage = $250 target notional
        # $250 / (10.0 * 0.20) = 125 contracts
        self.assertEqual(contracts, 125)

        # Committed margin should be exactly 125 * 2.0 / 50 = $5.0
        committed_margin = (contracts * 10.0 * entry_price) / cfg.leverage
        self.assertAlmostEqual(committed_margin, 5.0, places=2)

    def test_margin_safeguard_clamping(self):
        """Verify that sizing clamps if user requests margin exceeding wallet balance."""
        cfg = BacktestConfig(
            symbol="DOGE_USDT",
            volume_mode="MARGIN_PCT",
            margin_pct=150.0,  # exceeds balance
            leverage=50,
            initial_balance_usdt=50.0,
            timeframe="1m"
        )
        engine = BacktestExecutionEngine(config=cfg)
        entry_price = 0.20  # 1 contract = $2.0
        contracts = engine._resolve_simulated_contracts(entry_price)

        # Max affordable contracts with $50 balance at 50x leverage:
        # ($50 * 50) / 2.0 = 1250 contracts
        committed_margin = (contracts * 10.0 * entry_price) / cfg.leverage
        self.assertLessEqual(committed_margin, 50.0)


if __name__ == "__main__":
    unittest.main()
