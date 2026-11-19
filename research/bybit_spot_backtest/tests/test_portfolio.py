"""Portfolio accounting, Bybit examples, data alignment, and API regressions."""

import importlib
import os
import sqlite3
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


class PortfolioTests(unittest.TestCase):
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
        self.start = self.end - 120 * 60_000
        self.client = dashboard.app.test_client()
        self.owner = {"Tailscale-User-Login": "owner@example.test"}
        self.mutation = {
            **self.owner,
            "X-CSRF-Token": dashboard.csrf_token,
            "Origin": "https://nicholas-4.tail068b64.ts.net",
        }

    def params(self, n=30, **changes):
        result = {
            "strategy": "smart_portfolio",
            "start": self.start,
            "end": self.start + n * 60_000,
            "allocations": [
                {"inst_id": "BTCUSDT", "weight_pct": 50},
                {"inst_id": "ETHUSDT", "weight_pct": 30},
                {"inst_id": "SOLUSDT", "weight_pct": 20},
            ],
            "capital": 10_000,
            "fee_bps": 0,
            "slippage_bps": 0,
            "rebalance_mode": "threshold",
            "threshold_pct": 10,
            "interval_minutes": 5,
            "min_trade_usdt": 0,
        }
        result.update(changes)
        return result

    def bars(self, n=30, change=4, btc_after=1500):
        return [
            (
                self.start + i * 60_000,
                [1000 if i <= change else btc_after, 500, 100],
                [1000 if i < change else btc_after, 500, 100],
            )
            for i in range(n)
        ]

    def seed(self):
        with collector.open_db(self.db_path) as db:
            for inst, px in (("BTCUSDT", 1000), ("ETHUSDT", 500), ("SOLUSDT", 100)):
                collector.insert_rows(
                    db,
                    [
                        (
                            inst,
                            self.start + i * 60_000,
                            str(px),
                            str(px),
                            str(px),
                            str(px),
                            "1",
                            str(px),
                            self.end,
                        )
                        for i in range(120)
                    ],
                )

    def test_bybit_example_and_inclusive_percentage_point_threshold(self):
        result = worker.calculate_portfolio(self.params(), self.bars())
        self.assertEqual(result["rebalances"], 1)
        self.assertEqual(result["recent_rebalances"][0]["signal_ts_ms"], self.start + 4 * 60_000)
        self.assertEqual(result["recent_rebalances"][0]["ts_ms"], self.start + 5 * 60_000)
        expected = (4.166666666666667, 7.5, 25)
        for holding, qty in zip(result["holdings"], expected, strict=False):
            self.assertAlmostEqual(holding["final_qty"], qty, places=7)
        self.assertAlmostEqual(result["final_equity"], 12500, places=5)
        self.assertAlmostEqual(result["holdings"][1]["qty_change_pct"], 25, places=6)

    def test_absolute_deviation_not_relative_weight_change(self):
        # BTC weight 50% -> 55% is +5 percentage points, not a 10-point trigger.
        result = worker.calculate_portfolio(self.params(), self.bars(btc_after=1222.2222222222222))
        self.assertEqual(result["rebalances"], 0)
        self.assertEqual(result["order_count"], 3)

    def test_execution_uses_next_open_and_reprices_entire_portfolio(self):
        bars = self.bars()
        for i in range(5, len(bars)):
            bars[i] = (bars[i][0], [2000, 800, 110], [2000, 800, 110])
        result = worker.calculate_portfolio(self.params(), bars)
        rebalanced = [x for x in result["recent_orders"] if not x["initial_allocation"]]
        self.assertEqual(rebalanced[0]["ts_ms"], self.start + 5 * 60_000)
        self.assertEqual(rebalanced[0]["price"], 2000)
        self.assertAlmostEqual(result["holdings"][0]["final_qty"], 4.25, places=7)
        self.assertAlmostEqual(result["holdings"][1]["final_qty"], 6.375, places=7)

    def test_time_mode_waits_for_interval_and_requires_three_points(self):
        params = self.params(120, rebalance_mode="interval", threshold_pct=3, interval_minutes=60)
        result = worker.calculate_portfolio(params, self.bars(120))
        self.assertEqual(result["recent_rebalances"][0]["ts_ms"], self.start + 60 * 60_000)
        self.assertEqual(result["rebalance_checks"], 2)
        quiet = worker.calculate_portfolio(params, self.bars(120, btc_after=1080))
        self.assertEqual(quiet["rebalances"], 0)

    def test_final_close_signal_is_not_executed_without_next_bar(self):
        result = worker.calculate_portfolio(self.params(), self.bars(change=29))
        self.assertTrue(result["last_signal_unfilled"])
        self.assertEqual(result["rebalances"], 0)

    def test_fee_and_slippage_compounding_and_same_cost_benchmark(self):
        params = self.params(fee_bps=10, slippage_bps=5)
        bars = self.bars(btc_after=1000)
        result = worker.calculate_portfolio(params, bars)
        expected = 10000 * (1 - 0.0005) * (1 - 0.001) / ((1 + 0.0005) * (1 + 0.001))
        self.assertAlmostEqual(result["final_equity"], expected, places=5)
        self.assertEqual(result["final_equity"], result["buy_hold_final_equity"])
        self.assertAlmostEqual(result["fees_paid"], 10000 * 0.001 / 1.001, places=5)

    def test_rebalancing_conserves_cash_and_counts_all_fees(self):
        params = self.params(fee_bps=100, slippage_bps=100, threshold_pct=1)
        bars = [
            (
                self.start + i * 60_000,
                [1000 * (2 if (i // 5) % 2 else 0.5), 500, 100],
                [1000 * (2 if (i // 5) % 2 else 0.5), 500, 100],
            )
            for i in range(30)
        ]
        result = worker.calculate_portfolio(params, bars)
        self.assertLess(result["order_count"], 50)
        cash, wallet = 10000.0, {a["inst_id"]: 0.0 for a in params["allocations"]}
        for order in result["recent_orders"]:
            self.assertAlmostEqual(order["fee_usdt"], order["notional_usdt"] * 0.01)
            if order["side"] == "buy":
                cash -= order["notional_usdt"] + order["fee_usdt"]
                wallet[order["inst_id"]] += order["qty"]
            else:
                cash += order["notional_usdt"] - order["fee_usdt"]
                wallet[order["inst_id"]] -= order["qty"]
            self.assertGreaterEqual(cash, -1e-6)
            self.assertTrue(all(q >= -1e-10 for q in wallet.values()))
        self.assertAlmostEqual(cash, result["cash_usdt"], places=5)
        self.assertAlmostEqual(
            sum(x["fee_usdt"] for x in result["recent_orders"]), result["fees_paid"], places=5
        )
        self.assertAlmostEqual(
            sum(a["market_value_usdt"] for a in result["holdings"]) + cash,
            result["final_market_value"],
            places=5,
        )
        self.assertGreater(result["rebalances"], 0)

    def test_minimum_trade_and_initial_allocation_constraints(self):
        result = worker.calculate_portfolio(
            self.params(threshold_pct=0.1, min_trade_usdt=1000), self.bars(btc_after=1100)
        )
        self.assertEqual(result["rebalances"], 0)
        self.assertGreater(result["skipped_rebalances"], 0)
        with self.assertRaisesRegex(ValueError, "初始配置"):
            worker.calculate_portfolio(self.params(min_trade_usdt=6000), self.bars())

    def test_initial_minimum_rejected_before_job_creation_with_amounts(self):
        params = self.params(
            fee_bps=10,
            slippage_bps=5,
            min_trade_usdt=10000,
            allocations=[
                {"inst_id": "PEPEUSDT", "weight_pct": 50},
                {"inst_id": "DOGEUSDT", "weight_pct": 50},
            ],
        )
        with mock.patch.object(dashboard.executor, "submit") as submit:
            response = self.client.post("/api/backtests", json=params, headers=self.mutation)
        self.assertEqual(response.status_code, 400)
        self.assertIn("PEPEUSDT", response.json["error"])
        self.assertIn("4995.00", response.json["error"])
        self.assertIn("10000.00", response.json["error"])
        self.assertIn("20020.00", response.json["error"])
        submit.assert_not_called()
        self.assertEqual(list(dashboard.JOB_DIR.glob("*.json")), [])
        dashboard.validate_initial_allocations({**params, "min_trade_usdt": 5})

    def test_initial_minimum_boundary_accepts_small_coin_prices(self):
        params = self.params(
            min_trade_usdt=5000,
            allocations=[
                {"inst_id": "PEPEUSDT", "weight_pct": 50},
                {"inst_id": "DOGEUSDT", "weight_pct": 50},
            ],
        )
        dashboard.validate_initial_allocations(params)
        bars = [
            (self.start + i * 60000, [0.0000067, 0.01337], [0.0000067, 0.01337]) for i in range(30)
        ]
        result = worker.calculate_portfolio(params, bars)
        self.assertEqual(result["order_count"], 2)
        self.assertAlmostEqual(result["final_equity"], 10000, places=5)
        # Reserving fees can make a nominal 5,000 allocation insufficient.
        with self.assertRaisesRegex(ValueError, "4995.00"):
            dashboard.validate_initial_allocations({**params, "fee_bps": 10})

    def test_streamed_snapshot_alignment_and_fingerprint(self):
        self.seed()
        stream, digest = worker.load_portfolio_bars(self.params(120))
        result = worker.calculate_portfolio(self.params(120), stream, digest)
        self.assertEqual(result["bars"], 120)
        self.assertEqual(result["total_candles"], 360)
        self.assertEqual(len(result["data_sha256"]), 64)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "DELETE FROM candles_1m WHERE symbol='ETHUSDT' AND ts_ms=?",
                (self.start + 17 * 60_000,),
            )
        stream, digest = worker.load_portfolio_bars(self.params(120))
        with self.assertRaisesRegex(ValueError, "ETHUSDT.*缺少"):
            worker.calculate_portfolio(self.params(120), stream, digest)

    def test_invalid_price_rejects_whole_portfolio(self):
        self.seed()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE candles_1m SET c='nan' WHERE symbol='SOLUSDT' AND ts_ms=?", (self.start,)
            )
        stream, digest = worker.load_portfolio_bars(self.params(120))
        with self.assertRaisesRegex(ValueError, "SOLUSDT.*OHLC"):
            worker.calculate_portfolio(self.params(120), stream, digest)

    def test_parameter_validation_and_fixed_time_threshold(self):
        self.assertEqual(dashboard.portfolio_params(self.params())["interval_minutes"], 5)
        fixed = dashboard.portfolio_params(
            self.params(rebalance_mode="interval", threshold_pct=99, interval_minutes=240)
        )
        self.assertEqual(fixed["threshold_pct"], 3)
        invalid = [
            {"allocations": []},
            {"allocations": [{"inst_id": "BTCUSDT", "weight_pct": 50}] * 2},
            {
                "allocations": [
                    {"inst_id": "BTCUSDT", "weight_pct": 40},
                    {"inst_id": "ETHUSDT", "weight_pct": 50},
                ]
            },
            {"threshold_pct": "NaN"},
            {"threshold_pct": True},
            {"threshold_pct": 0},
            {"min_trade_usdt": "Infinity"},
            {"rebalance_mode": "invalid"},
            {"rebalance_mode": "interval", "interval_minutes": 0},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                dashboard.portfolio_params(self.params(**change))

    def test_owner_csrf_async_worker_history_and_both_modes(self):
        self.seed()
        payload = self.params(120, fee_bps=10, slippage_bps=5, min_trade_usdt=5)
        self.assertEqual(self.client.post("/api/backtests", json=payload).status_code, 403)
        self.assertEqual(
            self.client.post("/api/backtests", json=payload, headers=self.owner).status_code, 403
        )
        bad = self.client.post(
            "/api/backtests", json={**payload, "allocations": []}, headers=self.mutation
        )
        self.assertEqual(bad.status_code, 400)
        for mode in ("threshold", "interval"):
            payload["rebalance_mode"] = mode
            response = self.client.post("/api/backtests", json=payload, headers=self.mutation)
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json["id"]
            for _ in range(100):
                job = self.client.get("/api/backtests/" + job_id, headers=self.owner).json
                if job["state"] in ("done", "error"):
                    break
                time.sleep(0.05)
            self.assertEqual(job["state"], "done", job)
            self.assertEqual(job["result"]["engine_version"], 3)
            self.assertEqual(job["result"]["rebalance_mode"], mode)
            self.assertEqual(len(job["result"]["holdings"]), 3)
            self.assertEqual(len(job["result"]["data_sha256"]), 64)
            self.assertEqual(
                self.client.get(
                    "/api/backtests/" + job_id + "/download", headers=self.owner
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.delete("/api/backtests/" + job_id, headers=self.mutation).status_code,
                200,
            )


if __name__ == "__main__":
    unittest.main()
