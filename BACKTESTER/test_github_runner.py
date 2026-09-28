"""
Unit tests for GitHub Actions Cloud Backtester Runner
=====================================================
Tests workflow input generation, repository detection, artifact naming logic,
and GitHubBacktestRunner configuration.
"""

import os
import sys
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.config import BacktestConfig
from BACKTESTER.engine.github_runner import (
    GitHubBacktestRunner,
    get_git_remote_repo,
    resolve_github_token
)


class TestGitHubRunner(unittest.TestCase):

    def test_git_remote_repo_detection(self):
        owner, repo = get_git_remote_repo()
        self.assertEqual(owner, "SamratSinghPhysicist")
        self.assertEqual(repo, "KCEX_bot")

    def test_workflow_inputs_generation_trump(self):
        config = BacktestConfig(
            symbol="TRUMP_USDT",
            timeframe="1m",
            strategy_mode="STOCH_RSI",
            stoch_preset="FAST_SCALP",
            start_time="2026-01-01",
            end_time="2026-08-31",
            leverage=75,
            tp_ticks=2,
            sl_mode="ROE",
            sl_roe_pct=25.0,
            volume_mode="MULTIPLIER",
            volume_multiplier=2.0
        )
        runner = GitHubBacktestRunner()
        inputs = runner.build_workflow_inputs(config)

        self.assertEqual(inputs["symbol"], "TRUMP_USDT")
        self.assertEqual(inputs["timeframe"], "1m")
        self.assertEqual(inputs["strategy"], "STOCH_RSI")
        self.assertEqual(inputs["stoch_preset"], "FAST_SCALP")
        self.assertEqual(inputs["leverage"], "75")
        self.assertEqual(inputs["tp_ticks"], "2")
        self.assertEqual(inputs["sl_mode"], "ROE")
        self.assertEqual(inputs["sl_roe"], "25.0")
        self.assertEqual(inputs["volume_mode"], "MULTIPLIER")
        self.assertEqual(inputs["volume_multiplier"], "2.0")
        self.assertEqual(inputs["start_date"], "2026-01-01")
        self.assertEqual(inputs["end_date"], "2026-08-31")

    def test_workflow_inputs_generation_doge(self):
        config = BacktestConfig(
            symbol="DOGE_USDT",
            timeframe="5m",
            strategy_mode="EMA_CROSSOVER",
            ema_preset="5/13",
            leverage=75,
            tp_ticks=2,
            sl_mode="ROE",
            sl_roe_pct=25.0
        )
        runner = GitHubBacktestRunner()
        inputs = runner.build_workflow_inputs(config)

        self.assertEqual(inputs["symbol"], "DOGE_USDT")
        self.assertEqual(inputs["volume_multiplier"], "1.0")  # 1x min for DOGE
        self.assertEqual(inputs["leverage"], "75")

    def test_runner_api_url_and_headers(self):
        runner = GitHubBacktestRunner(owner="TestOwner", repo="TestRepo", token="test_token_123")
        self.assertEqual(runner.api_base, "https://api.github.com/repos/TestOwner/TestRepo")
        self.assertEqual(runner.headers["Authorization"], "Bearer test_token_123")
        self.assertIn("Accept", runner.headers)

    def test_workflow_inputs_generation_with_filters(self):
        import json
        config = BacktestConfig(
            symbol="TRUMP_USDT",
            timeframe="1m",
            strategy_mode="STOCH_RSI",
            duration_filter_enabled=True,
            duration_deep_monitor_seconds=45.0,
            duration_max_hold_seconds=75.0,
            duration_action="SCRATCH_OR_MARKET",
            adx_filter_enabled=True,
            adx_period=10,
            adx_threshold=25.0,
            htf_trend_filter_enabled=True,
            htf_ema_period=100,
            htf_timeframe="30m",
            hourly_filter_enabled=True,
            hourly_blacklist_utc=[0, 1, 23],
            direction_bias="LONG_ONLY",
            sl_price_pct=2.15
        )
        runner = GitHubBacktestRunner()
        inputs = runner.build_workflow_inputs(config)

        # Critical: GitHub Actions workflow_dispatch strictly enforces <= 25 inputs
        self.assertLessEqual(len(inputs), 25, f"Workflow inputs count {len(inputs)} exceeds GitHub 25-property limit")

        self.assertIn("filters_json", inputs)
        filters = json.loads(inputs["filters_json"])

        self.assertEqual(filters["duration_filter"], True)
        self.assertEqual(filters["duration_deep_monitor"], 45.0)
        self.assertEqual(filters["duration_max_hold"], 75.0)
        self.assertEqual(filters["duration_action"], "SCRATCH_OR_MARKET")
        self.assertEqual(filters["adx_filter"], True)
        self.assertEqual(filters["adx_period"], 10)
        self.assertEqual(filters["adx_threshold"], 25.0)
        self.assertEqual(filters["htf_trend_filter"], True)
        self.assertEqual(filters["htf_ema_period"], 100)
        self.assertEqual(filters["htf_timeframe"], "30m")
        self.assertEqual(filters["hourly_filter"], True)
        self.assertEqual(filters["hourly_blacklist"], "0,1,23")
        self.assertEqual(filters["direction_bias"], "LONG_ONLY")
        self.assertEqual(inputs["sl_price"], "2.15")


if __name__ == "__main__":
    unittest.main()
