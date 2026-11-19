"""Checks for the Top20 correlation snapshot generator."""

import importlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

builder = importlib.import_module("research.build_correlation")
market_config = importlib.import_module("market_config")

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def make_db(path, days=120, symbols=None, short_symbol_days=None):
    """Synthetic candles; ``short_symbol_days`` truncates one coin's history.

    The generator only reads hourly closes, so the fixtures store a handful of
    minutes per hour instead of every minute.
    """
    symbols = symbols or list(market_config.INSTRUMENTS)
    short_symbol_days = short_symbol_days or {}
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE candles_1m (
            symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, o TEXT, h TEXT, l TEXT,
            c TEXT, vol TEXT, turnover TEXT, received_at_ms INTEGER,
            PRIMARY KEY (symbol, ts_ms)
        ) WITHOUT ROWID;
        """
    )
    end = 200 * DAY_MS
    rows = []
    for index, symbol in enumerate(symbols):
        coin_days = short_symbol_days.get(symbol, days)
        start = end - coin_days * DAY_MS
        price = 100.0 + index
        for hour in range(start, end, HOUR_MS):
            for minute in (0, 30, 59):
                price *= 1 + 0.0001 * (index + 1) / 10
                ts = hour + minute * 60_000
                rows.append(
                    (symbol, ts, str(price), str(price), str(price), str(price), "1", "1", ts)
                )
    db.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return db, symbols


class CorrelationBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "market.sqlite3"
        self.db, self.symbols = make_db(self.path)
        self.addCleanup(self.db.close)
        self.turnover = {symbol: 1e6 * (index + 1) for index, symbol in enumerate(self.symbols)}

    def test_analysis_is_internally_consistent(self):
        analysis = builder.build_analysis(self.db, self.turnover)
        self.assertEqual(len(analysis["coins"]), 20)
        self.assertEqual(
            [row["symbol"] for row in analysis["ranking"]], analysis["coins"]
        )
        for days in ("30", "90"):
            matrix = analysis["matrices"][days]
            self.assertEqual(len(matrix), 20)
            for i, row in enumerate(matrix):
                self.assertEqual(len(row), 20)
                for j, value in enumerate(row):
                    self.assertEqual(value, matrix[j][i])
                    if value is not None:
                        self.assertGreaterEqual(value, -1.0000001)
                        self.assertLessEqual(value, 1.0000001)

    def test_ranking_follows_turnover_and_window_coverage(self):
        analysis = builder.build_analysis(self.db, self.turnover)
        volumes = [row["volume_24h_usdt"] for row in analysis["ranking"]]
        self.assertEqual(volumes, sorted(volumes, reverse=True))
        self.assertEqual([row["rank"] for row in analysis["ranking"]], list(range(1, 21)))
        for row in analysis["ranking"]:
            self.assertTrue(row["complete_90d"])
            self.assertGreaterEqual(row["samples_90d"], 90 * 24)

    def test_published_snapshot_passes_the_web_validator(self):
        from correlation_data import load_analysis

        analysis = builder.build_analysis(self.db, self.turnover)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot"
            builder.publish(target, analysis)
            loaded = load_analysis(target)
            self.assertEqual(loaded["coins"], analysis["coins"])
            for name in (
                "analysis.json",
                "top20-ranking.csv",
                "pair-correlations.csv",
                "correlation-30.csv",
                "correlation-90.csv",
                "correlation-common-161h.csv",
                "report.md",
                "bybit-top20-analysis.zip",
            ):
                self.assertTrue((target / name).is_file(), name)
            self.assertIn("BTC", (target / "report.md").read_text(encoding="utf-8"))

    def test_correlations_match_a_direct_computation(self):
        analysis = builder.build_analysis(self.db, self.turnover)
        closes = builder.hourly_closes(self.db)
        returns = builder.hourly_returns(closes)
        symbol = analysis["coins"][0]
        other = analysis["coins"][1]
        end = analysis["analysis_end_ms"]
        start = end - 30 * DAY_MS
        stamps = sorted(set(returns[symbol]) & set(returns[other]))
        stamps = [ts for ts in stamps if start < ts <= end]
        xs = [returns[symbol][ts] for ts in stamps]
        ys = [returns[other][ts] for ts in stamps]
        expected = builder.pearson(xs, ys)
        matrix = analysis["matrices"]["30"]
        i = analysis["coins"].index(symbol)
        j = analysis["coins"].index(other)
        self.assertAlmostEqual(matrix[i][j], expected, places=10)

    def test_missing_history_stays_null(self):
        # A coin with only a few hours of data cannot produce a correlation.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.sqlite3"
            short = self.symbols[-1]
            db, _ = make_db(path, short_symbol_days={short: 1})
            self.addCleanup(db.close)
            analysis = builder.build_analysis(db, self.turnover)
        index = analysis["coins"].index(short)
        self.assertIsNone(analysis["matrices"]["30"][index][0])
        self.assertIsNone(analysis["matrices"]["90"][index][0])
        row = next(r for r in analysis["ranking"] if r["symbol"] == short)
        self.assertFalse(row["complete_90d"])
        self.assertFalse(row["complete_30d"])


if __name__ == "__main__":
    unittest.main()
