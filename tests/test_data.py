"""Data integrity tests; all downloads are mocked and never contact providers."""
import json
import lzma
from pathlib import Path
import struct
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

try:
    from backend import data
except ModuleNotFoundError:
    from laboratorio.backend import data


class DataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = data.DATA_DIR
        data.DATA_DIR = Path(self.directory.name)

    def tearDown(self):
        data.DATA_DIR = self.previous
        self.directory.cleanup()

    def test_bid_csv_keeps_original_side_and_reports_estimation(self):
        content = "GMT time,Open,High,Low,Close\n02.01.2025 00:00:00.000,1.1,1.2,1,1.15\n02.01.2025 00:01:00.000,1.15,1.2,1.1,1.16\n"
        meta = data.import_csv(content, "Dukascopy import", "bid", 2)
        bars, restored = data.load_dataset(meta["id"])
        self.assertFalse(meta["has_bid_ask"])
        self.assertFalse(meta["synthetic"])
        self.assertEqual(meta["price_type"], "bid")
        self.assertEqual(meta["timeframe"], "1m")
        self.assertEqual(bars[0]["bid_open"], 1.1)
        self.assertAlmostEqual(bars[0]["ask_open"], 1.1002)
        self.assertAlmostEqual(bars[0]["open"], 1.1001)
        self.assertEqual(restored["rows"], 2)
        self.assertEqual(data.list_datasets()[0]["id"], meta["id"])

    def test_joined_bid_ask_are_observed_and_midpoint_is_explicit(self):
        content = "time,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close\n2025-01-02T00:00:00+02:00,1.1,1.2,1,1.15,1.1002,1.2002,1.0002,1.1502\n2025-01-02T00:01:00+02:00,1.1,1.2,1,1.15,1.1002,1.2002,1.0002,1.1502\n"
        meta = data.import_csv(content, "quotes")
        bars, _ = data.load_dataset(meta["id"])
        self.assertTrue(meta["has_bid_ask"])
        self.assertEqual(meta["canonical_price_type"], "mid")
        self.assertEqual(bars[0]["time"], "2025-01-01T22:00:00Z")
        self.assertAlmostEqual(bars[0]["open"], 1.1001)

    def test_jpy_pip_and_symbol_are_preserved(self):
        csv = "time,open,high,low,close\n2025-01-02T00:00:00Z,150,151,149,150.1\n2025-01-02T00:01:00Z,150.1,151,149,150.2"
        meta = data.import_csv(csv, "JPY", "bid", 2, "USD/JPY")
        bars, _ = data.load_dataset(meta["id"])
        self.assertEqual(meta["symbol"], "USDJPY")
        self.assertAlmostEqual(bars[0]["ask_open"] - bars[0]["bid_open"], 0.02)

    def test_sort_dedup_last_row_and_weekend_quality(self):
        csv = "time,open,high,low,close\n2025-01-05T22:00:00Z,1,2,.5,1.3\n2025-01-03T21:59:00Z,1,2,.5,1.2\n2025-01-03T21:58:00Z,1,2,.5,1.1\n2025-01-05T22:00:00Z,1,2,.5,1.4"
        meta = data.import_csv(csv, "duplicate", "mid")
        bars, _ = data.load_dataset(meta["id"])
        self.assertEqual(meta["rows"], 3)
        self.assertEqual(meta["quality"]["duplicate_rows"], 1)
        self.assertFalse(meta["quality"]["sorted_input"])
        self.assertEqual(meta["quality"]["unexpected_missing_bars"], 0)
        self.assertEqual(meta["quality"]["weekend_missing_bars"], 2880)
        self.assertEqual(bars[-1]["close"], 1.4)

    def test_weekday_missing_bar_is_not_filled(self):
        csv = "time,open,high,low,close\n2025-01-02T00:00:00Z,1,2,.5,1\n2025-01-02T00:01:00Z,1,2,.5,1\n2025-01-02T00:03:00Z,1,2,.5,1"
        meta = data.import_csv(csv, "gap")
        self.assertEqual(meta["rows"], 3)
        self.assertEqual(meta["quality"]["unexpected_missing_bars"], 1)
        self.assertEqual(meta["quality"]["filled_bars"], 0)

    def test_rejects_bad_prices_ohlc_and_paths(self):
        for values in ("nan,2,.5,1", "1,2,-1,1", "1,.5,.1,1", "1,inf,.5,1"):
            with self.subTest(values=values), self.assertRaises(ValueError):
                data.import_csv("time,open,high,low,close\n2025-01-02T00:00:00Z," + values, "bad")
        with self.assertRaises(ValueError):
            data.load_dataset("../outside")

    def test_metatrader_separate_dates_and_tab_separator(self):
        csv = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\n2025.01.02\t00:00:00\t1\t2\t.5\t1\n2025.01.02\t01:00:00\t1\t2\t.5\t1"
        meta = data.import_csv(csv, "MT5")
        self.assertEqual(meta["timeframe"], "1h")

    def test_decoder_order_scale_and_zero_volume(self):
        raw = struct.pack(">5If", 60, 110000, 110003, 109995, 110010, 3.0)
        raw += struct.pack(">5If", 120, 110003, 110003, 110003, 110003, 0.0)
        bars = data._decode_minute_bi5(lzma.compress(raw), date(2025, 1, 2))
        self.assertEqual(len(bars), 1)
        candle = bars["2025-01-02T00:01:00Z"]
        self.assertEqual(candle, {"open": 1.1, "high": 1.1001, "low": 1.09995, "close": 1.10003})
        jpy = data._decode_minute_bi5(lzma.compress(raw), date(2025, 1, 2), "USDJPY")
        self.assertEqual(next(iter(jpy.values()))["open"], 110)

    def test_decoder_rejects_tick_or_wrong_time_base(self):
        for raw in (b"x" * 20, struct.pack(">5If", 86400, 1, 1, 1, 1, 1.0), struct.pack(">5If", 1, 1, 1, 1, 1, 1.0)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                data._decode_minute_bi5(lzma.compress(raw), date(2025, 1, 2))

    def test_download_exclusive_end_join_and_missing_quote_report(self):
        calls = []
        def fetch(day, side, symbol):
            calls.append((day, side, symbol))
            delta = 0.0002 if side == "ASK" else 0
            quotes = {f"{day}T00:00:00Z": {"open": 1 + delta, "high": 1.1 + delta, "low": .9 + delta, "close": 1 + delta}}
            if side == "BID":
                quotes[f"{day}T00:01:00Z"] = quotes[f"{day}T00:00:00Z"].copy()
            return quotes, "cache"
        progress = []
        with patch.object(data, "_fetch_day_side", side_effect=fetch):
            meta = data.download_dukascopy("2025-01-02", "2025-01-04", progress.append, "GBPUSD")
        self.assertEqual(meta["rows"], 2)
        self.assertEqual(meta["symbol"], "GBPUSD")
        self.assertEqual(meta["quality"]["unmatched_quote_minutes"], 2)
        self.assertEqual({c[0] for c in calls}, {date(2025, 1, 2), date(2025, 1, 3)})
        self.assertEqual(progress[-1]["progress"], 100)

    def test_rate_limiting_fails_truthfully(self):
        with patch.object(data, "_fetch_day_side", side_effect=RuntimeError("HTTP 429")):
            with self.assertRaisesRegex(RuntimeError, "429"):
                data.download_dukascopy("2025-01-02", "2025-01-03")
        self.assertEqual(data.list_datasets(), [])

    def test_demo_is_labeled_and_reproducible(self):
        meta = data.create_demo()
        bars, _ = data.load_dataset(meta["id"])
        self.assertTrue(meta["synthetic"])
        self.assertFalse(meta["has_bid_ask"])
        self.assertEqual(meta["rows"], 18794)
        self.assertEqual(data.create_demo()["id"], meta["id"])
        self.assertTrue(all(b["ask_open"] >= b["bid_open"] > 0 for b in bars))


if __name__ == "__main__":
    unittest.main()
