"""Execution, causality, position sizing, and public API checks for Turtle."""

import importlib
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "web"), str(ROOT)]
os.environ["BYBIT_WEB_OWNER"] = "owner@example.test"
_bootstrap = tempfile.TemporaryDirectory()
os.environ["BYBIT_WEB_JOBS"] = _bootstrap.name
dashboard = importlib.import_module("app")
worker = importlib.import_module("backtest_worker")
turtle = importlib.import_module("turtle_strategy")


class TurtleTests(unittest.TestCase):
    def params(self, **kwargs):
        return {
            "strategy": "turtle",
            "inst_id": "ETHUSDT",
            "capital": 10000,
            "fee_bps": 0,
            "slippage_bps": 0,
            "signal_minutes": 60,
            "entry_period": 5,
            "exit_period": 2,
            "atr_period": 2,
            "risk_pct": 1,
            "stop_n": 2,
            "add_n": 0.5,
            "max_units": 4,
            "min_trade_usdt": 5,
            **kwargs,
        }

    def bars(self, suffix=None, hours=7):
        prices = [(100, 101, 99, 100)] * 300 + [(104, 104, 104, 104)] * 60
        prices += [(105, 105, 104, 105)] * (hours * 60 - len(prices))
        for index, row in (suffix or {}).items():
            prices[index] = row
        return [(i * 60000, *row) for i, row in enumerate(prices)]

    def test_prior_channel_next_minute_entry_atr_and_unspent_cash(self):
        result = worker.calculate(self.params(max_units=1), self.bars(), "digest")
        order = result["recent_orders"][0]
        self.assertEqual(order["ts_ms"], 360 * 60000)
        self.assertEqual(order["price"], 105)
        self.assertAlmostEqual(order["qty"], 100 / 3)
        self.assertEqual(result["position_n"], 3)
        self.assertEqual(result["stop_price"], 99)
        self.assertEqual(result["cash_usdt"], 6500)
        self.assertEqual(result["final_equity"], 10000)
        self.assertEqual(result["data_sha256"], "digest")
        self.assertEqual(result["engine_version"], 4)

    def test_half_n_pyramids_four_unit_limit_and_gap_stop(self):
        bars = self.bars(
            {
                361: (106, 106.6, 105.5, 106),
                362: (108, 108, 107, 108),
                363: (110, 110, 109, 110),
                364: (112, 112, 111, 112),
                365: (97, 98, 96, 97),
            }
        )
        result = worker.calculate(self.params(risk_pct=0.5), bars)
        orders = result["recent_orders"]
        self.assertEqual([o["reason"] for o in orders], ["entry", "add", "add", "add", "stop"])
        self.assertEqual([o["price"] for o in orders], [105, 106.5, 108, 110, 97])
        self.assertEqual(result["max_units_seen"], 4)
        self.assertEqual(result["additions"], 3)
        self.assertEqual(result["completed_trades"], 1)
        self.assertEqual(result["stop_exits"], 1)
        self.assertAlmostEqual(orders[-1]["pnl"], (97 * 4 - 105 - 106.5 - 108 - 110) * 50 / 3)

    def test_existing_stop_precedes_add_and_new_stop_is_conservative(self):
        result = worker.calculate(self.params(), self.bars({361: (105, 108, 98, 105)}))
        self.assertEqual([o["reason"] for o in result["recent_orders"]], ["entry", "stop"])
        self.assertEqual(result["recent_orders"][-1]["price"], 99)
        # Open occurs before the intrabar add. A raised stop cannot fill at that earlier open.
        result = worker.calculate(
            self.params(add_n=2, stop_n=0.5), self.bars({361: (105, 112, 104, 110)})
        )
        orders = result["recent_orders"]
        self.assertEqual([o["reason"] for o in orders], ["entry", "add", "stop"])
        self.assertEqual(orders[1]["price"], 111)
        self.assertEqual(orders[2]["price"], 109.5)

    def test_channel_exit_is_confirmed_and_next_open_fills(self):
        bars = self.bars(hours=10)
        for i in range(420, 540):
            bars[i] = (i * 60000, 103, 103, 103, 103)
        bars[540] = (540 * 60000, 102, 102, 102, 102)
        result = worker.calculate(self.params(max_units=1, stop_n=5), bars)
        self.assertEqual(result["recent_orders"][-1]["reason"], "channel_exit")
        self.assertEqual(result["recent_orders"][-1]["ts_ms"], 480 * 60000)
        self.assertEqual(result["recent_orders"][-1]["price"], 103)
        self.assertEqual(result["channel_exits"], 1)

    def test_fee_slippage_cash_conservation_and_liquidation_valuation(self):
        p = self.params(max_units=1, fee_bps=10, slippage_bps=5)
        result = worker.calculate(p, self.bars())
        order = result["recent_orders"][0]
        q = 100 / 3
        px = 105 * 1.0005
        fee = q * px * 0.001
        cash = 10000 - q * px - fee
        self.assertAlmostEqual(order["fee_usdt"], fee)
        self.assertAlmostEqual(result["fees_paid"], fee, places=4)
        self.assertAlmostEqual(result["cash_usdt"], cash, delta=0.000051)
        self.assertAlmostEqual(result["final_equity"], cash + q * 105 * 0.9995 * 0.999, places=4)
        self.assertAlmostEqual(result["turnover_usdt"], q * px, places=4)
        self.assertAlmostEqual(result["slippage_cost_usdt"], q * (px - 105), places=4)
        sold = worker.calculate(p, self.bars({361: (95, 95, 94, 95)}))
        self.assertEqual(sold["completed_trades"], 1)
        self.assertAlmostEqual(
            sold["fees_paid"], sum(o["fee_usdt"] for o in sold["recent_orders"]), places=4
        )
        self.assertAlmostEqual(
            sold["cash_usdt"], 10000 + sold["recent_orders"][-1]["pnl"], places=4
        )

    def test_cash_limits_minimum_notional_and_no_zero_atr_trade(self):
        # Tiny N would otherwise demand more cash than the initial balance.
        bars = [(i * 60000, 100, 100.01, 99.99, 100) for i in range(420)]
        for i in range(300, 360):
            bars[i] = (i * 60000, 100.02, 100.02, 100.02, 100.02)
        result = worker.calculate(self.params(fee_bps=10), bars)
        self.assertEqual(result["cash_usdt"], 0)
        self.assertEqual(result["cash_limited_orders"], 1)
        self.assertEqual(result["order_count"], 1)
        self.assertLess(
            result["recent_orders"][0]["qty"] * result["recent_orders"][0]["price"], 10000
        )
        self.assertEqual(
            worker.calculate(self.params(min_trade_usdt=5000), self.bars())["skipped_entries"], 1
        )
        flat = [(i * 60000, 100, 100, 100, 100) for i in range(420)]
        result = worker.calculate(self.params(), flat)
        self.assertEqual(result["order_count"], 0)
        self.assertTrue(math.isfinite(result["final_equity"]))

    def test_partial_signal_bars_and_last_signal_never_execute_early(self):
        bars = self.bars()[:360]
        result = worker.calculate(self.params(), bars)
        self.assertEqual(result["order_count"], 0)
        self.assertTrue(result["last_signal_unfilled"])
        # The first partial hour must not contribute to channels or ATR.
        bars = self.bars(hours=9)[30:]
        result = worker.calculate(self.params(max_units=1), bars)
        self.assertEqual(result["signal_bars"], 8)
        self.assertEqual(result["recent_orders"][0]["ts_ms"], 420 * 60000)
        # Future prices cannot change earlier execution decisions.
        original = self.bars(hours=9)
        changed = original[:420] + [(i * 60000, 1, 1, 1, 1) for i in range(420, 540)]
        before = worker.calculate(self.params(max_units=1), original)["recent_orders"]
        after = worker.calculate(self.params(max_units=1), changed)["recent_orders"]
        self.assertEqual(
            [x for x in before if x["ts_ms"] < 420 * 60000],
            [x for x in after if x["ts_ms"] < 420 * 60000],
        )

    def test_validation_and_api_job_parameter_roundtrip(self):
        for patch in (
            {"signal_minutes": 1},
            {"risk_pct": float("nan")},
            {"max_units": 5},
            {"entry_period": True},
            {"exit_period": 55},
            {"atr_period": 1},
            {"min_trade_usdt": -1},
            {"add_n": "__import__('os')"},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                turtle.turtle_params(patch)
        with self.assertRaisesRegex(ValueError, "56"):
            turtle.turtle_params({"start": 0, "end": 30 * 86400000})
        now = int(time.time() // 60) * 60000
        body = self.params(start=now - 8 * 3600000, end=now)
        headers = {
            "Tailscale-User-Login": "owner@example.test",
            "X-CSRF-Token": dashboard.csrf_token,
        }
        with tempfile.TemporaryDirectory() as folder:
            jobs = Path(folder)
            with (
                mock.patch.object(dashboard, "JOB_DIR", jobs),
                mock.patch.object(dashboard, "RUN_LOCK", jobs / ".lock"),
                mock.patch.object(dashboard.executor, "submit") as submit,
            ):
                response = dashboard.app.test_client().post(
                    "/api/backtests", json=body, headers=headers
                )
                self.assertEqual(response.status_code, 202, response.text)
                job, fd = submit.call_args.args[1:]
                os.close(fd)
                self.assertEqual(job["params"]["strategy"], "turtle")
                self.assertEqual(job["params"]["entry_period"], 5)
                self.assertEqual(job["params"]["signal_minutes"], 60)
                self.assertEqual(dashboard.load_job(job["id"])["params"], job["params"])
                bad = dashboard.app.test_client().post(
                    "/api/backtests", json={**body, "exit_period": 5}, headers=headers
                )
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(
                    dashboard.app.test_client().post("/api/backtests", json=body).status_code, 403
                )


if __name__ == "__main__":
    unittest.main()
