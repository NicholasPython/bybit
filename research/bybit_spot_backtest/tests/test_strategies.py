"""Nine strategy causality, indicator, accounting and isolated API regressions."""

import importlib
import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "web"), str(ROOT)]
os.environ["BYBIT_WEB_OWNER"] = "owner@example.test"
_bootstrap = tempfile.TemporaryDirectory()
os.environ["BYBIT_WEB_JOBS"] = _bootstrap.name
dashboard = importlib.import_module("app")
worker = importlib.import_module("backtest_worker")
collector = importlib.import_module("collector")
from strategy_catalog import STRATEGIES, strategy_params  # noqa: E402
from strategy_engine import (  # noqa: E402
    EMA,
    Aggregate,
    Book,
    Signals,
    allocation,
    bands,
    calculate_research,
    pearson,
)


class StrategyTests(unittest.TestCase):
    def params(self, strategy, **changes):
        p = {
            "strategy": strategy,
            "inst_id": "BTCUSDT",
            "capital": 10000,
            "fee_bps": 10,
            "slippage_bps": 5,
            "signal_minutes": 60,
            "universe": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT"],
            **changes,
        }
        p.update(strategy_params(p))
        return p

    def rows(self, values, start=0, volume_spike=None):
        """Expand independent hourly fixtures to complete minute candles."""
        for hour, c in enumerate(values):
            for minute in range(60):
                v = 100 if hour == volume_spike else 10
                yield start + (hour * 60 + minute) * 60000, [(c, c + 1, max(0.001, c - 1), c, v)]

    def test_wilder_ema_population_bands_and_correlation(self):
        e = EMA(3, True)
        self.assertEqual([e.update(v) for v in (1, 2, 3)], [None, None, 2])
        self.assertAlmostEqual(e.update(5), 3)
        b = bands([1, 2, 3, 4, 5], 5, 2)
        self.assertEqual(b[0], 3)
        self.assertAlmostEqual(b[2], 3 + 2 * math.sqrt(2))
        self.assertAlmostEqual(pearson([1, 2, 3], [3, 2, 1]), -1)
        self.assertIsNone(pearson([1, 1, 1], [2, 3, 4]))

    def test_all_five_indicator_strategies_generate_distinct_expected_signals(self):
        cases = {
            "bollinger_reversion": (
                [100] * 5 + [80, 90, 100],
                {"band_period": 5, "band_std": 1, "atr_period": 2},
                6,
            ),
            "trend_pullback": (
                list(range(100, 130)) + [127.9, 130, 131],
                {"pullback_period": 2, "trend_fast": 3, "trend_slow": 5, "atr_period": 2},
                31,
            ),
            "macd_adx": (
                [100] * 6 + [99, 98, 97, 96, 95, 96, 98, 100, 102, 104, 103, 101, 99],
                {
                    "macd_fast": 2,
                    "macd_slow": 3,
                    "macd_signal": 2,
                    "adx_period": 2,
                    "adx_threshold": 5,
                    "atr_period": 2,
                },
                11,
            ),
            "supertrend": (
                [100, 100, 100, 105, 106, 107, 90, 89],
                {"atr_period": 2, "multiplier": 1},
                3,
            ),
            "squeeze_breakout": (
                [100] * 20 + [110, 100],
                {
                    "band_period": 5,
                    "squeeze_lookback": 10,
                    "volume_period": 2,
                    "atr_period": 2,
                    "volume_multiple": 1.5,
                },
                20,
            ),
        }
        for strategy, (values, patch, index) in cases.items():
            with self.subTest(strategy=strategy):
                p = self.params(strategy, **patch)
                signals = Signals(p)
                results = [
                    signals.update((i * 3600000, c, c + 1, c - 1, c, 100 if i == 20 else 10))[0]
                    for i, c in enumerate(values)
                ]
                self.assertEqual(results[index], "buy")
                # A signal close fills on the next hour's first minute, never its own minute.
                result = calculate_research(p, list(self.rows(values, volume_spike=20)))
                self.assertGreater(result["order_count"], 0)
                self.assertEqual(result["recent_orders"][0]["ts_ms"], (index + 1) * 3600000)

    def test_squeeze_requires_real_past_volume_and_adx_filters_weak_trend(self):
        p = self.params(
            "squeeze_breakout", band_period=5, squeeze_lookback=10, volume_period=2, atr_period=2
        )
        result = calculate_research(p, list(self.rows([100] * 20 + [110, 100])))
        self.assertEqual(result["order_count"], 0)
        p = self.params(
            "macd_adx", macd_fast=2, macd_slow=3, macd_signal=2, adx_period=2, atr_period=2
        )
        signals = Signals(p)
        self.assertFalse(
            any(
                signals.update((i * 3600000, 100, 101, 99, 100, 10))[0] == "buy" for i in range(100)
            )
        )

    def test_book_round_trip_costs_cash_and_partial_realized_basis(self):
        book = Book(self.params("budget_dca"), ["BTCUSDT"])
        bought = book.buy(0, 0, 100, 1000, "test")
        q = 1000 / (100 * 1.0005 * 1.001)
        self.assertAlmostEqual(bought[0], q)
        self.assertEqual(book.cash, 9000)
        book.sell(60000, 0, 110, q / 2, "test")
        proceeds = q / 2 * 110 * 0.9995 * 0.999
        self.assertAlmostEqual(book.cash, 9000 + proceeds)
        self.assertAlmostEqual(book.cost[0], 500)
        self.assertAlmostEqual(book.realized, proceeds - 500)
        self.assertAlmostEqual(book.fees, sum(o["fee_usdt"] for o in book.orders))
        book.buy(120000, 0, 110, 1e9, "test")
        self.assertAlmostEqual(book.cash, 0)
        self.assertTrue(all(q >= 0 for q in book.qty))

    def test_fixed_budget_no_external_flows_finite_tranches_and_unused_cash(self):
        p = self.params("budget_dca", tranches=2, dca_minutes=60, fee_bps=0, slippage_bps=0)
        rows = [(i * 60000, [(100, 100, 100, 100, 1)]) for i in range(180)]
        result = calculate_research(p, rows)
        self.assertEqual(result["dca_buys"], 2)
        self.assertEqual(result["cash_usdt"], 0)
        self.assertEqual(result["final_equity"], 10000)
        self.assertEqual([o["ts_ms"] for o in result["recent_orders"]], [0, 3600000])
        short = calculate_research(p, rows[:30])
        self.assertEqual(short["cash_usdt"], 5000)
        self.assertEqual(short["final_equity"], 10000)

    def test_grid_fixed_levels_cost_basis_and_no_same_minute_roundtrip(self):
        p = self.params(
            "spot_grid",
            lower_pct=80,
            upper_pct=120,
            grid_count=2,
            grid_mode="arithmetic",
            fee_bps=0,
            slippage_bps=0,
        )
        rows = [
            (0, [(100, 100, 100, 100, 1)]),
            (60000, [(100, 125, 75, 85, 1)]),
            (120000, [(85, 105, 85, 100, 1)]),
        ]
        result = calculate_research(p, rows)
        self.assertEqual(result["grid_levels"], [80, 100, 120])
        self.assertEqual([o["side"] for o in result["recent_orders"]], ["buy", "sell"])
        self.assertEqual(result["recent_orders"][0]["ts_ms"], 60000)
        self.assertEqual(result["recent_orders"][1]["ts_ms"], 120000)
        self.assertEqual(result["grid_rounds"], 1)
        self.assertEqual(result["final_equity"], 11250)
        self.assertEqual(result["realized_pnl_usdt"], 1250)

    def test_momentum_uses_only_past_ranks_and_falls_back_to_cash(self):
        p = self.params("momentum_rotation", short_lookback=2, long_lookback=3, hold_count=1)
        histories = [
            [100, 101, 102, 110],
            [100, 105, 106, 108],
            [100, 99, 98, 97],
            [100] * 4,
            [100] * 4,
        ]
        w, _ = allocation(p, histories)
        self.assertEqual(w, [1, 0, 0, 0, 0])
        w, _ = allocation(p, [[100, 99, 98, 97]] * 5)
        self.assertEqual(w, [0] * 5)
        reversed_pool = list(reversed(p["universe"]))
        self.assertEqual(
            strategy_params({**p, "universe": reversed_pool})["universe"], reversed_pool
        )

    def test_low_correlation_selection_rejects_correlated_assets_and_caps_weight(self):
        p = self.params(
            "low_corr_portfolio",
            corr_lookback=10,
            hold_count=3,
            max_correlation=0.3,
            max_weight_pct=40,
        )
        returns = [
            [0.001 * math.sin(i) for i in range(10)],
            [0.002 * math.sin(i) for i in range(10)],
            [0.003 * math.cos(i) for i in range(10)],
        ]
        histories = []
        for series in returns:
            values = [100]
            for x in series:
                values.append(values[-1] * math.exp(x))
            histories.append(values)
        histories += [[100] * 11] * 2
        weights, _ = allocation(p, histories)
        self.assertGreater(weights[0], 0)
        self.assertEqual(weights[1], 0)
        self.assertLessEqual(max(weights), 0.4)
        self.assertLess(sum(weights), 1)

    def test_partial_aggregation_last_close_and_future_invariance(self):
        a = Aggregate(60, 1)
        for i in range(30, 60):
            self.assertIsNone(a.update(i * 60000, [(1, 2, 0.5, 1, 1)]))
        complete = None
        for i in range(60, 120):
            complete = a.update(i * 60000, [(1, 2, 0.5, 1, 1)])
        self.assertEqual(complete, [(3600000, 1, 2, 0.5, 1, 60)])
        p = self.params("bollinger_reversion", band_period=5, band_std=1, atr_period=2)
        rows = list(self.rows([100] * 5 + [80, 90]))
        result = calculate_research(p, rows)
        self.assertEqual(result["order_count"], 0)
        self.assertTrue(result["last_signal_unfilled"])
        original = list(self.rows([100] * 5 + [80, 90, 100, 101]))
        changed = original[:480] + [(ts, [(1, 1, 1, 1, 1)]) for ts, _ in original[480:]]
        before, after = calculate_research(p, original), calculate_research(p, changed)
        self.assertEqual(
            [x for x in before["recent_orders"] if x["ts_ms"] < 480 * 60000],
            [x for x in after["recent_orders"] if x["ts_ms"] < 480 * 60000],
        )

    def test_invalid_and_short_params_rejected(self):
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy), self.assertRaises(ValueError):
                self.params(strategy, min_trade_usdt=float("nan"))
        for strategy, patch in (
            ("trend_pullback", {"trend_fast": 300, "trend_slow": 100}),
            ("macd_adx", {"macd_fast": 50, "macd_slow": 20}),
            ("momentum_rotation", {"universe": ["BTCUSDT"]}),
            ("spot_grid", {"grid_count": True}),
            ("budget_dca", {"dca_minutes": 1}),
        ):
            with self.assertRaises(ValueError):
                self.params(strategy, **patch)
        with self.assertRaisesRegex(ValueError, "完整"):
            self.params("trend_pullback", start=0, end=3600000)

    def test_all_nine_api_jobs_run_in_isolated_worker_and_benchmark(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dashboard.DB_PATH = root / "data.sqlite3"
            dashboard.JOB_DIR = root / "jobs"
            dashboard.JOB_DIR.mkdir()
            dashboard.RUN_LOCK = dashboard.JOB_DIR / ".lock"
            worker.DB_PATH = str(dashboard.DB_PATH)
            end = int(time.time() // 3600) * 3600000
            start = end - 260 * 3600000
            universe = self.params("momentum_rotation")["universe"]
            with collector.open_db(dashboard.DB_PATH) as db:
                for index, asset in enumerate(universe):

                    def rows(index=index, asset=asset):
                        for i in range(260 * 60):
                            c = 100 * math.exp(
                                0.001 * i / 60 + 0.08 * math.sin(i / (60 * (index + 2)))
                            )
                            yield (
                                asset,
                                start + i * 60000,
                                str(c),
                                str(c * 1.001),
                                str(c * 0.999),
                                str(c),
                                "10",
                                "10",
                                end,
                            )

                    collector.insert_rows(db, rows())
            client = dashboard.app.test_client()
            headers = {
                "Tailscale-User-Login": "owner@example.test",
                "X-CSRF-Token": dashboard.csrf_token,
            }
            for strategy in STRATEGIES:
                with self.subTest(strategy=strategy):
                    p = self.params(strategy, start=start, end=end)
                    response = client.post("/api/backtests", json=p, headers=headers)
                    self.assertEqual(response.status_code, 202, response.text)
                    job_id = response.json["id"]
                    for _ in range(600):
                        job = client.get("/api/backtests/" + job_id, headers=headers).json
                        if job["state"] in ("done", "error"):
                            break
                        time.sleep(0.01)
                    self.assertEqual(job["state"], "done", job.get("error"))
                    result = job["result"]
                    self.assertEqual(result["strategy"], strategy)
                    self.assertEqual(result["engine_version"], 5)
                    self.assertEqual(result["bars"], 15600)
                    self.assertEqual(len(result["data_sha256"]), 64)
                    self.assertLessEqual(len(result["equity_curve"]), 501)
                    self.assertGreaterEqual(result["cash_usdt"], 0)
                    self.assertEqual(
                        client.get(
                            f"/api/backtests/{job_id}/download", headers=headers
                        ).status_code,
                        200,
                    )
            response = client.get(
                f"/api/backtests/{job_id}/benchmark?inst=ETHUSDT", headers=headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json["benchmark"]["assumptions"]["exit_cost_method"], "compound"
            )
            self.assertEqual(client.post("/api/backtests", json=p).status_code, 403)
            html = client.get("/", headers=headers).text
            for name in STRATEGIES:
                self.assertIn('value="' + name + '"', html)
            with sqlite3.connect(dashboard.DB_PATH) as db:
                db.execute(
                    "DELETE FROM candles_1m WHERE symbol='BTCUSDT' AND ts_ms=?", (start + 60000,)
                )
            stream, _ = worker.stream_research_bars(p)
            with self.assertRaisesRegex(ValueError, "缺少"):
                list(stream)


if __name__ == "__main__":
    unittest.main()
