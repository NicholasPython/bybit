"""Exercise all twenty real instrument identifiers through the strategy API."""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "web")]
os.environ["BYBIT_WEB_OWNER"] = "owner@example.test"
_jobs = tempfile.TemporaryDirectory()
os.environ["BYBIT_WEB_JOBS"] = _jobs.name
dashboard = importlib.import_module("app")
worker = importlib.import_module("backtest_worker")
collector = importlib.import_module("collector")
config = importlib.import_module("market_config")


class BodyParser(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.attrs = dict(attrs)


class Top20Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "market.sqlite3"
        jobs = root / "jobs"
        jobs.mkdir()
        for obj, key, value in (
            (dashboard, "DB_PATH", self.db),
            (worker, "DB_PATH", str(self.db)),
            (dashboard, "JOB_DIR", jobs),
            (dashboard, "RUN_LOCK", jobs / ".backtest.lock"),
        ):
            patch = mock.patch.object(obj, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.end = int(time.time() // 60) * 60000
        self.start = self.end - 30 * 60000
        with collector.open_db(self.db) as db:
            for i, inst in enumerate(config.SUPPORTED_INSTRUMENTS):
                price = str(100 + i)
                collector.insert_rows(
                    db,
                    [
                        (
                            inst,
                            self.start + n * 60000,
                            price,
                            price,
                            price,
                            price,
                            "1",
                            "1",
                            self.end,
                        )
                        for n in range(30)
                    ],
                )
        self.client = dashboard.app.test_client()
        self.owner = {"Tailscale-User-Login": "owner@example.test"}
        self.mutation = {**self.owner, "X-CSRF-Token": dashboard.csrf_token}

    def portfolio(self):
        return {
            "strategy": "smart_portfolio",
            "start": self.start,
            "end": self.end,
            "capital": 20000,
            "fee_bps": 10,
            "slippage_bps": 5,
            "allocations": [{"inst_id": inst, "weight_pct": 5} for inst in config.INSTRUMENTS],
            "rebalance_mode": "threshold",
            "threshold_pct": 5,
            "interval_minutes": 5,
            "min_trade_usdt": 5,
        }

    def submit(self, payload):
        response = self.client.post("/api/backtests", json=payload, headers=self.mutation)
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(100):
            job = self.client.get("/api/backtests/" + response.json["id"], headers=self.owner).json
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "done", job)
        return job

    def test_page_status_and_collector_use_same_twenty(self):
        parser = BodyParser()
        parser.feed(self.client.get("/", headers=self.owner).text)
        frontend = json.loads(parser.attrs["data-instruments"])
        status = self.client.get("/api/status", headers=self.owner).json
        self.assertEqual(frontend, [row["inst_id"] for row in status["pairs"]])
        self.assertEqual(len(frontend), 20)
        self.assertIn("HOODXUSDT", frontend)
        self.assertIn("XAUTUSDT", frontend)
        self.assertNotIn("DOGEUSDT", frontend)
        self.assertTrue(set(frontend).issubset(collector.INSTRUMENTS))

    def test_twenty_asset_job_runs_in_isolated_worker_with_costs(self):
        job = self.submit(self.portfolio())
        result = job["result"]
        self.assertEqual(len(result["holdings"]), 20)
        self.assertEqual(result["total_candles"], 600)
        self.assertEqual(result["order_count"], 20)
        self.assertEqual(result["rebalances"], 0)
        self.assertAlmostEqual(
            result["final_equity"], 20000 * 0.9995 * 0.999 / (1.0005 * 1.001), places=5
        )
        self.assertEqual({h["inst_id"] for h in result["holdings"]}, set(config.INSTRUMENTS))

    def test_new_coin_single_strategies_and_legacy_people_remain_valid(self):
        for strategy in ("sma_cross", "rsi_reversion"):
            params = {
                "strategy": strategy,
                "inst_id": "LITUSDT",
                "start": self.start,
                "end": self.end,
                "capital": 10000,
                "fee_bps": 10,
                "slippage_bps": 5,
                "fast": 2,
                "slow": 5,
                "period": 5,
                "buy_below": 30,
                "sell_above": 70,
            }
            job = self.submit(params)
            self.assertEqual(job["result"]["inst_id"], "LITUSDT")
        for inst in ("AVAXUSDT", "DOGEUSDT"):
            response = self.client.get(
                "/api/backtests/" + job["id"] + "/benchmark?inst=" + inst, headers=self.owner
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json["benchmark"]["inst_id"], inst)

    def test_missing_new_coin_minute_and_unsupported_instruments_fail(self):
        params = self.portfolio()
        with sqlite3.connect(self.db) as db:
            db.execute(
                "DELETE FROM candles_1m WHERE symbol='HOODXUSDT' AND ts_ms=?", (self.start + 60000,)
            )
        stream, digest = worker.load_portfolio_bars(params)
        with self.assertRaisesRegex(ValueError, "HOODXUSDT.*缺少"):
            worker.calculate_portfolio(params, stream, digest)
        bad = {
            **params,
            "allocations": params["allocations"] + [{"inst_id": "DOGEUSDT", "weight_pct": 5}],
        }
        self.assertEqual(
            self.client.post("/api/backtests", json=bad, headers=self.mutation).status_code, 400
        )
        with self.assertRaisesRegex(ValueError, "交易对无效"):
            worker.stream_single_bars({"inst_id": "FAKEUSDT"})


if __name__ == "__main__":
    unittest.main()
