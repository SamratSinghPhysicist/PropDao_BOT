"""
Unit & Integration Tests for Backtest Analytics Engine
"""
import sys
import unittest

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from BACKTESTER.analytics.indexer import ReportIndexer
from BACKTESTER.analytics.engine import AnalyticsEngine


class TestAnalyticsEngine(unittest.TestCase):

    def setUp(self):
        self.indexer = ReportIndexer()
        self.engine = AnalyticsEngine(self.indexer)

    def test_indexing(self):
        runs = self.indexer.get_all_runs()
        self.assertGreater(len(runs), 0, "Should discover at least one backtest run")
        for r in runs:
            self.assertTrue(r.metadata.symbol, "Symbol should not be empty")
            self.assertGreater(r.scorecard.total_trades, 0, "Trades count should be > 0")

    def test_comparison(self):
        runs = self.indexer.get_all_runs()
        run_ids = [r.metadata.run_id for r in runs]
        cmp_result = self.engine.compare_runs(run_ids)
        self.assertIn("parameter_diffs", cmp_result)
        self.assertIn("comparison_matrix", cmp_result)
        self.assertIn("equity_overlays", cmp_result)
        self.assertIn("radar_footprints", cmp_result)
        self.assertIn("exit_comparison", cmp_result)

    def test_parameter_extraction(self):
        runs = self.indexer.get_all_runs()
        self.assertGreater(len(runs), 0)
        for r in runs:
            self.assertIsNotNone(r.metadata.parameters)
            self.assertIsNotNone(r.metadata.filters)
            self.assertTrue(r.metadata.strategy_preset or "preset" in r.metadata.parameters)
            self.assertTrue(r.metadata.timeframe, f"Timeframe should be populated for {r.metadata.run_id}")
            self.assertTrue(r.metadata.date_range, f"Date range should be populated for {r.metadata.run_id}")
            self.assertIn("2026", r.metadata.date_range)
            self.assertTrue(r.metadata.start_date, f"Start date should be populated for {r.metadata.run_id}")
            self.assertTrue(r.metadata.end_date, f"End date should be populated for {r.metadata.run_id}")
            self.assertGreater(r.metadata.contract_size, 0)
            self.assertGreater(r.metadata.price_unit, 0)
            if "EMA" in r.metadata.strategy.upper():
                self.assertIn("ema_fast", r.metadata.parameters)
                self.assertIn("ema_slow", r.metadata.parameters)
            elif "STOCH" in r.metadata.strategy.upper():
                self.assertIn("stoch_rsi_period", r.metadata.parameters)

    def test_parameter_diff_detection(self):
        runs = self.indexer.get_all_runs()
        if len(runs) >= 2:
            run_ids = [runs[0].metadata.run_id, runs[1].metadata.run_id]
            cmp_result = self.engine.compare_runs(run_ids)
            diffs = cmp_result["parameter_diffs"]
            self.assertIsInstance(diffs, list)
            self.assertGreater(len(diffs), 0)
            # Check structure of diff items
            for d in diffs:
                self.assertIn("key", d)
                self.assertIn("name", d)
                self.assertIn("values", d)
                self.assertIn("is_diff", d)

    def test_ai_dossier_exporter(self):
        from BACKTESTER.analytics.exporter import AIDossierExporter
        exporter = AIDossierExporter(self.engine)
        runs = self.indexer.get_all_runs()
        if runs:
            md = exporter.export_single_markdown(runs[0])
            self.assertIn("Hyperparameters & Configuration", md)
            self.assertIn(runs[0].metadata.strategy, md)

            json_dossier = exporter.export_json_dossier(runs[:2])
            self.assertEqual(json_dossier["schema_version"], "2.0.0")
            self.assertIn("runs", json_dossier)
            self.assertIn("parameters", json_dossier["runs"][0]["metadata"])


if __name__ == "__main__":
    unittest.main()
