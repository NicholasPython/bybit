"""Calendar bounds, long-lived jobs, and streaming backtest regression tests."""

import datetime as dt
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "web")]
os.environ["BYBIT_WEB_OWNER"] = "owner@example.test"
_bootstrap = tempfile.TemporaryDirectory()
os.environ["BYBIT_WEB_JOBS"] = _bootstrap.name
dashboard = importlib.import_module("app")
worker = importlib.import_module("backtest_worker")
config = importlib.import_module("backtest_config")


def timestamp(value):
    return int(dt.datetime.fromisoformat(value).replace(tzinfo=dt.UTC).timestamp() * 1000)


class LongRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        dashboard.JOB_DIR = Path(self.temp.name)
        dashboard.RUN_LOCK = dashboard.JOB_DIR / ".backtest.lock"
        self.client = dashboard.app.test_client()
        self.owner = {"Tailscale-User-Login": "owner@example.test"}
        self.mutation = {**self.owner, "X-CSRF-Token": dashboard.csrf_token}

    def test_exact_five_calendar_years_with_leap_days(self):
        for start, end in [
            ("2019-03-01", "2024-03-01"),
            ("2020-02-29", "2025-02-28"),
            ("2021-10-06", "2026-10-06"),
        ]:
            opened, closed = timestamp(start), timestamp(end)
            config.validate_backtest_window(opened, closed)
            self.assertEqual(
                dashboard.valid_window(
                    {"start": opened, "end": closed}, dashboard.MAX_BACKTEST_MINUTES
                ),
                (opened, closed),
            )
            with self.assertRaisesRegex(ValueError, "最多 5 年"):
                config.validate_backtest_window(opened, closed + 60000)
        with self.assertRaisesRegex(ValueError, "30 分钟"):
            config.validate_backtest_window(
                timestamp("2026-01-01"), timestamp("2026-01-01") + 29 * 60000
            )

    def test_api_accepts_five_years_and_rejects_one_extra_minute(self):
        params = {
            "strategy": "sma_cross",
            "inst_id": "BTCUSDT",
            "fast": 20,
            "slow": 60,
            "capital": 10000,
            "fee_bps": 10,
            "slippage_bps": 5,
            "start": timestamp("2021-10-06"),
            "end": timestamp("2026-10-06"),
        }
        with mock.patch.object(
            dashboard.executor, "submit", side_effect=lambda fn, job, fd: os.close(fd)
        ):
            response = self.client.post("/api/backtests", headers=self.mutation, json=params)
        self.assertEqual(response.status_code, 202)
        bad = {**params, "end": params["end"] + 60000}
        response = self.client.post("/api/backtests", headers=self.mutation, json=bad)
        self.assertEqual(response.status_code, 400)
        self.assertIn("5 年", response.get_json()["error"])

    def test_multi_minute_running_job_is_not_marked_stale(self):
        now = int(time.time() * 1000)
        job = {
            "id": "c" * 24,
            "state": "running",
            "params": {},
            "created_at_ms": now - 120000,
            "updated_at_ms": now - 120000,
        }
        dashboard.save_job(job)
        response = self.client.get("/api/backtests/" + job["id"], headers=self.owner)
        self.assertEqual(response.get_json()["state"], "running")
        self.assertGreater(dashboard.JOB_STALE_MS, config.BACKTEST_TIMEOUT_SECONDS * 1000)

    def test_single_coin_strategies_consume_one_pass_and_bound_output(self):
        start, n = timestamp("2025-01-01"), 10000
        bars = [(start + i * 60000, 100 + i % 30, 135, 95, 100 + (i + 1) % 30) for i in range(n)]
        for strategy in ("sma_cross", "rsi_reversion"):
            params = {
                "inst_id": "BTCUSDT",
                "strategy": strategy,
                "start": start,
                "end": start + n * 60000,
                "capital": 10000,
                "fee_bps": 10,
                "slippage_bps": 5,
                "fast": 2,
                "slow": 7,
                "period": 5,
                "buy_below": 30,
                "sell_above": 70,
            }
            stream = (row for row in bars)
            result = worker.calculate(params, stream)
            self.assertEqual(result["bars"], n)
            self.assertLessEqual(len(result["equity_curve"]), 501)
            self.assertLessEqual(len(result["recent_orders"]), 30)
            self.assertGreater(result["order_count"], 30)
            self.assertTrue(result["orders_truncated"])
            self.assertEqual(list(stream), [])
            self.assertEqual(result["equity_curve"][-1][0], params["end"] - 60000)
            self.assertEqual(result, worker.calculate(params, bars))

    def test_streaming_benchmark_and_short_stream_rejection(self):
        start = timestamp("2025-01-01")
        params = {
            "inst_id": "BTCUSDT",
            "start": start,
            "end": start + 144000 * 60000,
            "capital": 10000,
            "fee_bps": 0,
            "slippage_bps": 0,
        }
        bars = ((start + i * 60000, 100, 110, 100, 110) for i in range(144000))
        result = worker.calculate_buy_hold(params, bars)
        self.assertEqual(result["bars"], 144000)  # More than the former 90-day ceiling.
        self.assertEqual(result["final_equity"], 11000)
        self.assertLessEqual(len(result["equity_curve"]), 501)
        with self.assertRaisesRegex(ValueError, "不完整"):
            worker.calculate_buy_hold(params, iter([(start, 100, 100, 100, 100)]))


if __name__ == "__main__":
    unittest.main()
