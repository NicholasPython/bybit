"""Fair-cost buy-and-hold comparison and completed-job API integration."""

import importlib
import os
import sqlite3
import subprocess
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
collector = importlib.import_module("collector")


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "market.sqlite3"
        dashboard.DB_PATH = self.db_path
        dashboard.JOB_DIR = Path(self.temp.name) / "jobs"
        dashboard.JOB_DIR.mkdir()
        dashboard.RUN_LOCK = dashboard.JOB_DIR / ".backtest.lock"
        worker.DB_PATH = str(self.db_path)
        self.end = int(time.time() // 60) * 60_000
        self.start = self.end - 30 * 60_000
        self.params = {
            "inst_id": "BTCUSDT",
            "start": self.start,
            "end": self.end,
            "capital": 10000,
            "fee_bps": 10,
            "slippage_bps": 5,
        }
        self.bars = [(self.start + i * 60_000, 100, 120, 80, 110) for i in range(30)]
        self.client = dashboard.app.test_client()
        self.owner = {"Tailscale-User-Login": "owner@example.test"}
        self.job_id = "b" * 24

    def seed_job(self, engine=3, state="done"):
        with collector.open_db(self.db_path) as db:
            collector.insert_rows(
                db,
                [
                    ("BTCUSDT", ts, str(o), str(h), str(low), str(c), "1", "1", self.end)
                    for ts, o, h, low, c in self.bars
                ],
            )
        # Benchmark BTC is deliberately outside the saved ETH/SOL portfolio.
        params = {
            **self.params,
            "strategy": "smart_portfolio",
            "allocations": [
                {"inst_id": "ETHUSDT", "weight_pct": 50},
                {"inst_id": "SOLUSDT", "weight_pct": 50},
            ],
        }
        params.pop("inst_id")
        result = {"engine_version": engine, "return_pct": 20, "final_equity": 12000}
        job = {
            "id": self.job_id,
            "state": state,
            "params": params,
            "result": result,
            "created_at_ms": self.end,
            "updated_at_ms": self.end,
        }
        dashboard.save_job(job)
        return dashboard.JOB_DIR / f"{self.job_id}.json"

    def compare(self, inst="BTCUSDT", **kwargs):
        return self.client.get(
            f"/api/backtests/{self.job_id}/benchmark?inst={inst}", headers=self.owner, **kwargs
        )

    def test_first_open_full_capital_and_compounded_costs(self):
        result = worker.calculate_buy_hold(self.params, self.bars, "fingerprint")
        expected = 10000 * 1.1 * 0.9995 * 0.999 / (1.0005 * 1.001)
        self.assertAlmostEqual(result["final_equity"], expected, places=5)
        self.assertAlmostEqual(result["quantity"], 10000 / (100 * 1.0005 * 1.001))
        self.assertAlmostEqual(result["entry_fee_usdt"], 10000 * 0.001 / 1.001, places=5)
        self.assertEqual(result["equity_curve"][-1][0], self.end - 60000)
        self.assertEqual(result["end_ms"], self.end)
        self.assertEqual(result["data_sha256"], "fingerprint")
        self.assertFalse(result["assumptions"]["forced_exit"])

    def test_legacy_saved_strategy_preserves_valuation_convention(self):
        result = worker.calculate_buy_hold({**self.params, "valuation_version": 2}, self.bars)
        expected = 10000 * 1.1 * (1 - 0.001 - 0.0005) / (1.0005 * 1.001)
        self.assertAlmostEqual(result["final_equity"], expected, places=5)
        self.assertEqual(result["assumptions"]["exit_cost_method"], "legacy_additive")

    def test_drawdown_uses_every_minute_not_sampled_curve(self):
        bars = [(self.start + i * 60000, 100, 200, 50, 100) for i in range(1000)]
        bars[1] = (bars[1][0], 100, 200, 50, 200)
        bars[2] = (bars[2][0], 100, 200, 50, 50)
        result = worker.calculate_buy_hold({**self.params, "fee_bps": 0, "slippage_bps": 0}, bars)
        self.assertEqual(result["max_drawdown_pct"], 75)
        self.assertLessEqual(len(result["equity_curve"]), 501)

    def test_existing_result_compares_outside_portfolio_without_mutation(self):
        path = self.seed_job()
        before = path.read_bytes()
        data = self.compare().get_json()
        benchmark = data["benchmark"]
        self.assertEqual(benchmark["inst_id"], "BTCUSDT")
        self.assertEqual(benchmark["start_ms"], self.start)
        self.assertEqual(benchmark["end_ms"], self.end)
        self.assertEqual(benchmark["capital"], 10000)
        self.assertEqual(len(benchmark["data_sha256"]), 64)
        self.assertAlmostEqual(data["excess_return_pp"], 20 - benchmark["return_pct"], places=5)
        self.assertEqual(path.read_bytes(), before)
        lock = dashboard.acquire_run_lock()
        self.assertIsNotNone(lock)
        os.close(lock)

    def test_old_engine_job_works_without_new_result_fields(self):
        self.seed_job(engine=2)
        response = self.compare()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["benchmark"]["assumptions"]["exit_cost_method"], "legacy_additive"
        )

    def test_identity_state_and_coin_validation(self):
        self.seed_job(state="running")
        url = f"/api/backtests/{self.job_id}/benchmark?inst=BTCUSDT"
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.compare().status_code, 409)
        self.seed_job()
        self.assertEqual(self.compare("BOGUSUSDT").status_code, 400)
        self.assertEqual(
            self.client.get("/api/backtests/../../etc/benchmark", headers=self.owner).status_code,
            404,
        )

    def test_missing_listing_data_and_internal_gap_are_not_filled(self):
        self.seed_job()
        response = self.compare("TRUMPUSDT")
        self.assertEqual(response.status_code, 422)
        self.assertIn("TRUMPUSDT", response.get_json()["error"])
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM candles_1m WHERE ts_ms=?", (self.start + 60000,))
        self.assertEqual(self.compare().status_code, 422)

    def test_shared_resource_lock_and_timeout_release(self):
        self.seed_job()
        lock = dashboard.acquire_run_lock()
        try:
            self.assertEqual(self.compare().status_code, 429)
        finally:
            os.close(lock)
        with mock.patch.object(
            dashboard.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 65)
        ):
            self.assertEqual(self.compare().status_code, 504)
        self.assertEqual(self.compare().status_code, 200)


if __name__ == "__main__":
    unittest.main()
