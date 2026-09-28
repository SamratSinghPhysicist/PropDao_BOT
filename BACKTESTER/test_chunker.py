"""
Unit & Integration Tests for Chronos Slicer & AI Dossier Generator
"""
import sys
import os
import unittest
import zipfile

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.analytics.indexer import ReportIndexer
from BACKTESTER.analytics.indicators import (
    compute_rsi_series,
    compute_bollinger_bands,
    compute_macd_series,
    compute_choppiness_index,
    compute_volume_surge_series,
    compute_vwap_series,
    compute_candle_anatomy,
    IndicatorMatrix
)
from BACKTESTER.analytics.chunker import ChronosChunker


class TestChronosChunker(unittest.TestCase):

    def setUp(self):
        self.indexer = ReportIndexer()
        self.chunker = ChronosChunker(indexer=self.indexer)
        self.runs = self.indexer.get_all_runs()
        self.assertGreater(len(self.runs), 0, "Should have at least one backtest run in reports")
        self.test_run = self.runs[0]

    def test_indicator_calculations(self):
        # Synthetic test data
        closes = [10.0 + (i * 0.1) + (math_sin := (i % 5) * 0.05) for i in range(50)]
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.2 for c in closes]
        volumes = [100.0 + (i * 5.0) for i in range(50)]

        # 1. RSI
        rsi = compute_rsi_series(closes, 14)
        self.assertEqual(len(rsi), 50)
        for r in rsi:
            self.assertTrue(0.0 <= r <= 100.0)

        # 2. Bollinger Bands
        up, mid, low, pct_b, width = compute_bollinger_bands(closes, 20)
        self.assertEqual(len(up), 50)
        self.assertGreaterEqual(up[-1], mid[-1])
        self.assertGreaterEqual(mid[-1], low[-1])

        # 3. MACD
        macd, sig, hist = compute_macd_series(closes)
        self.assertEqual(len(macd), 50)

        # 4. Choppiness Index
        chop = compute_choppiness_index(highs, lows, closes, 14)
        self.assertEqual(len(chop), 50)

        # 5. Volume Surge
        surge = compute_volume_surge_series(volumes, 20)
        self.assertEqual(len(surge), 50)

        # 6. VWAP
        vwap, dist = compute_vwap_series(highs, lows, closes, volumes)
        self.assertEqual(len(vwap), 50)

        # 7. Candle Anatomy
        anatomy = compute_candle_anatomy(10.0, 10.5, 9.8, 10.3)
        self.assertTrue(anatomy["is_bullish"])
        self.assertAlmostEqual(anatomy["total_range"], 0.7, places=4)
        self.assertAlmostEqual(anatomy["body_size"], 0.3, places=4)

    def test_indicator_matrix(self):
        candles = [
            {
                "time": 1700000000 + (i * 60),
                "open": 10.0 + (i * 0.05),
                "high": 10.2 + (i * 0.05),
                "low": 9.9 + (i * 0.05),
                "close": 10.1 + (i * 0.05),
                "volume": 500.0 + (i * 10)
            }
            for i in range(60)
        ]
        matrix = IndicatorMatrix(candles)
        snap = matrix.get_snapshot(30)
        self.assertIn("trend", snap)
        self.assertIn("momentum", snap)
        self.assertIn("volatility_and_bands", snap)
        self.assertIn("volume_and_fair_value", snap)
        self.assertIn("candle_microstructure", snap)
        self.assertEqual(snap["trend"]["adx_regime"], snap["trend"]["adx_regime"])

    def test_chunk_manifest_monthly(self):
        manifest = self.chunker.get_chunk_manifest(self.test_run.metadata.run_id, granularity="monthly")
        self.assertEqual(manifest["run_id"], self.test_run.metadata.run_id)
        self.assertEqual(manifest["granularity"], "monthly")
        self.assertGreater(manifest["total_chunks"], 0)
        first_chunk = manifest["chunks"][0]
        self.assertTrue(first_chunk["chunk_id"])
        self.assertGreater(first_chunk["trades_count"], 0)

    def test_chunk_manifest_loss_clusters(self):
        manifest = self.chunker.get_chunk_manifest(self.test_run.metadata.run_id, granularity="loss_clusters")
        self.assertEqual(manifest["granularity"], "loss_clusters")
        self.assertIsInstance(manifest["chunks"], list)

    def test_chunk_extraction_and_markdown(self):
        manifest = self.chunker.get_chunk_manifest(self.test_run.metadata.run_id, granularity="monthly")
        c0 = manifest["chunks"][0]
        chunk_data = self.chunker.extract_chunk_data(
            run_id=self.test_run.metadata.run_id,
            start_ms=c0["start_ms"],
            end_ms=c0["end_ms"],
            max_losing_trades=5,
            include_ticks=True,
            include_post_exit=True
        )
        self.assertIn("scorecard", chunk_data)
        self.assertIn("regime_profile", chunk_data)
        self.assertIn("losing_autopsies", chunk_data)

        # Format markdown
        md = self.chunker.format_chunk_markdown(chunk_data, chunk_index=1, total_chunks=len(manifest["chunks"]))
        self.assertIn("# 🤖 AI Quantitative Deep-Analysis Dossier", md)
        self.assertIn("Chunk Performance Scorecard", md)
        self.assertIn("Forensic Autopsy of Losing Trades", md)
        self.assertIn("Guided Quantitative Research Directives for AI", md)

        # Format JSON
        json_data = self.chunker.format_chunk_json(chunk_data, chunk_index=1, total_chunks=len(manifest["chunks"]))
        self.assertEqual(json_data["schema_version"], "2.1.0")
        self.assertIn("data", json_data)

    def test_batch_zip_export(self):
        zip_buf = self.chunker.export_all_chunks_zip(
            run_id=self.test_run.metadata.run_id,
            granularity="monthly",
            max_losing_trades=3,
            include_ticks=False
        )
        with zipfile.ZipFile(zip_buf, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("00_README_AND_MASTER_SYNTHESIS_PROMPT.md", namelist)
            self.assertIn("manifest.json", namelist)
            has_chunk_files = any(f.startswith("chunk_") and f.endswith(".md") for f in namelist)
            self.assertTrue(has_chunk_files, "ZIP must contain chunk markdown files")


if __name__ == "__main__":
    unittest.main()
