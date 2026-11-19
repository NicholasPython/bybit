"""Focused security, data-quality, backup, and backtest regression checks."""

import csv
import hashlib
import importlib
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))
os.environ["BYBIT_WEB_OWNER"] = "owner@example.test"
_bootstrap_jobs = tempfile.TemporaryDirectory()
os.environ["BYBIT_WEB_JOBS"] = _bootstrap_jobs.name

collector = importlib.import_module("collector")
backup_db = importlib.import_module("backup_db")
dashboard = importlib.import_module("app")
worker = importlib.import_module("backtest_worker")


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db_path = root / "market.sqlite3"
        self.jobs = root / "jobs"
        self.jobs.mkdir()
        dashboard.DB_PATH = self.db_path
        dashboard.JOB_DIR = self.jobs
        dashboard.RUN_LOCK = self.jobs / ".backtest.lock"
        dashboard.COLLECTOR = ROOT / "collector.py"
        dashboard.SYNC_LOCK = root / "sync.lock"
        worker.DB_PATH = str(self.db_path)
        self.end = int(time.time() // 60) * 60_000
        self.start = self.end - 120 * 60_000
        with collector.open_db(self.db_path) as db:
            rows = [
                (
                    "BTCUSDT",
                    self.start + i * 60_000,
                    "100",
                    "102",
                    "99",
                    str(100 + i / 100),
                    "1",
                    "100",
                    self.end,
                )
                for i in range(120)
            ]
            collector.insert_rows(db, rows)
            db.execute(
                "INSERT INTO ingest_state(symbol,updated_at_ms) VALUES (?,?)", ("BTCUSDT", self.end)
            )
        self.client = dashboard.app.test_client()
        self.headers = {"Tailscale-User-Login": "owner@example.test"}
        self.mutation_headers = {
            **self.headers,
            "X-CSRF-Token": dashboard.csrf_token,
            "Origin": "https://nicholas-4.tail068b64.ts.net",
        }

    def query(self, path):
        return self.client.get(
            path + f"?inst=BTCUSDT&start={self.start}&end={self.end}", headers=self.headers
        )

    def test_owner_authentication_and_mutation_protection(self):
        self.assertEqual(self.client.get("/api/status").status_code, 403)
        self.assertEqual(
            self.client.get(
                "/api/status", headers={"Tailscale-User-Login": "other@example.test"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/api/status", headers=self.headers).status_code, 200)
        self.assertEqual(
            self.client.post("/api/ingest/sync", headers=self.headers).status_code, 403
        )
        wrong_origin = {**self.mutation_headers, "Origin": "https://evil.example"}
        self.assertEqual(
            self.client.post("/api/ingest/sync", headers=wrong_origin).status_code, 403
        )
        self.assertEqual(
            self.client.get("/api/backtests/not-an-id", headers=self.headers).status_code, 404
        )

    def test_quality_export_and_corrupt_bar_rejection(self):
        quality = self.query("/api/quality")
        self.assertEqual(quality.status_code, 200)
        self.assertEqual(quality.json["missing"], 0)
        self.assertEqual(quality.json["invalid"], 0)
        export = self.query("/api/candles.csv")
        self.assertEqual(export.status_code, 200)
        self.assertEqual(len(list(csv.reader(io.StringIO(export.text)))), 121)
        too_wide = self.client.get(
            f"/api/candles.csv?inst=BTCUSDT&start={self.end - 8 * 86_400_000}&end={self.end}",
            headers=self.headers,
        )
        self.assertEqual(too_wide.status_code, 400)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "DELETE FROM candles_1m WHERE symbol=? AND ts_ms=?",
                ("BTCUSDT", self.start + 50 * 60_000),
            )
            db.execute(
                "UPDATE candles_1m SET h='0' WHERE symbol=? AND ts_ms=?",
                ("BTCUSDT", self.start + 51 * 60_000),
            )
        quality = self.query("/api/quality").json
        self.assertEqual((quality["missing"], quality["invalid"]), (1, 1))
        self.assertEqual(quality["gaps"][0]["minutes"], 1)
        with self.assertRaisesRegex(ValueError, "不完整"):
            worker.load_bars({"inst_id": "BTCUSDT", "start": self.start, "end": self.end})

    def test_backtest_history_download_and_delete(self):
        payload = {
            "inst_id": "BTCUSDT",
            "strategy": "sma_cross",
            "start": self.start,
            "end": self.end,
            "fast": 5,
            "slow": 10,
            "capital": 10000,
            "fee_bps": 10,
            "slippage_bps": 5,
        }
        response = self.client.post("/api/backtests", json=payload, headers=self.mutation_headers)
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json["id"]
        for _ in range(100):
            job = self.client.get(f"/api/backtests/{job_id}", headers=self.headers).json
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["result"]["bars"], 120)
        self.assertEqual(job["result"]["engine_version"], 2)
        self.assertEqual(len(job["result"]["data_sha256"]), 64)
        self.assertEqual(
            self.client.get("/api/backtests", headers=self.headers).json["jobs"][0]["id"], job_id
        )
        self.assertEqual(
            self.client.get(f"/api/backtests/{job_id}/download", headers=self.headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete(f"/api/backtests/{job_id}", headers=self.headers).status_code, 403
        )
        self.assertEqual(
            self.client.delete(
                f"/api/backtests/{job_id}", headers=self.mutation_headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/api/backtests/{job_id}", headers=self.headers).status_code, 404
        )

    def test_stale_job_recovery_and_manual_sync(self):
        job = {
            "id": "a" * 24,
            "state": "running",
            "params": {},
            "created_at_ms": 1,
            "updated_at_ms": 1,
        }
        dashboard.save_job(job)
        recovered = self.client.get("/api/backtests/" + job["id"], headers=self.headers).json
        self.assertEqual(recovered["state"], "error")
        with mock.patch.object(
            dashboard.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout="sync ok", stderr=""),
        ) as run:
            response = self.client.post("/api/ingest/sync", headers=self.mutation_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.call_args.args[0][-1], "sync")
        held_fd = dashboard.acquire_run_lock()
        self.assertIsNotNone(held_fd)
        try:
            self.assertIsNone(dashboard.acquire_run_lock())
        finally:
            os.close(held_fd)

    def test_manual_sync_forwards_proxy_and_transport_environment(self):
        # A minimal subprocess environment drops the proxy settings and the
        # collector transport, so the public API calls would time out.
        with mock.patch.dict(
            os.environ,
            {
                "https_proxy": "http://127.0.0.1:7890",
                "http_proxy": "http://127.0.0.1:7890",
                "BYBIT_COLLECTOR_SSH_HOST": "",
            },
        ):
            env = dashboard.sync_env()
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(env["BYBIT_COLLECTOR_SSH_HOST"], "")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertNotIn("SECRET", env)

    def test_next_open_execution_and_flat_rsi(self):
        bars = [
            (i * 60_000, opened, max(opened, close), min(opened, close), close)
            for i, (opened, close) in enumerate([(1, 1), (1, 1), (1, 1), (2, 2), (3, 2)])
        ]
        params = {
            "inst_id": "BTCUSDT",
            "strategy": "sma_cross",
            "fast": 2,
            "slow": 3,
            "capital": 1000,
            "fee_bps": 0,
            "slippage_bps": 0,
        }
        result = worker.calculate(params, bars)
        self.assertEqual(result["recent_orders"][0]["ts_ms"], 4 * 60_000)
        self.assertEqual(result["recent_orders"][0]["price"], 3)
        self.assertEqual(list(worker.rsi_signals([100] * 20, 5, 30, 70)), [None] * 20)

    def test_collector_repair_validation_and_verified_backup(self):
        raw = [str(self.start + 50 * 60_000), "100", "101", "99", "100", "1", "100"]
        with self.assertRaisesRegex(RuntimeError, "OHLC"):
            collector.parse_rows("BTCUSDT", [[raw[0], "100", "90", "99", "100", "1", "100"]])
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "DELETE FROM candles_1m WHERE symbol=? AND ts_ms=?",
                ("BTCUSDT", self.start + 50 * 60_000),
            )

        server_ms = self.end

        class FakeClient:
            def candle_pages(self, requests):
                return [
                    collector.CandlePage(symbol, end, [raw], None, server_ms)
                    for symbol, end in requests
                ]

        with collector.open_db(self.db_path) as db:
            self.assertIsNotNone(collector.first_gap(db, "BTCUSDT"))
            collector.repair_gaps(db, FakeClient(), 2)
            self.assertIsNone(collector.first_gap(db, "BTCUSDT"))
        destination = Path(self.temp.name) / "backups"
        path, digest = backup_db.backup(self.db_path, destination)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        with sqlite3.connect(path) as restored:
            self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0], 120)

    def test_backfill_target_stops_at_the_requested_minute(self):
        target = self.start + 98 * 60_000
        path = Path(self.temp.name) / "target.sqlite3"

        server_ms = self.end

        class FakeClient:
            def __init__(self):
                self.calls = []

            def candle_pages(self, requests):
                self.calls.append(requests)
                rows = [
                    [str(target + delta * 60_000), "100", "101", "99", "100", "1", "100"]
                    for delta in (1, 0, -1)
                ]
                return [
                    collector.CandlePage(symbol, end, rows, None, server_ms)
                    for symbol, end in requests
                ]

        client = FakeClient()
        with collector.open_db(path) as db:
            db.executemany(
                "INSERT INTO ingest_state"
                "(symbol,backfill_cursor_ms,backfill_complete,updated_at_ms) VALUES (?,?,?,?)",
                [
                    (inst, target + 2 * 60_000, int(inst != "BTCUSDT"), self.end)
                    for inst in collector.INSTRUMENTS
                ],
            )
            with mock.patch.object(collector, "refresh_instruments"):
                collector.backfill(db, client, 10, target)
            self.assertEqual(client.calls, [[("BTCUSDT", target + 2 * 60_000)]])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0], 2)
            self.assertEqual(
                db.execute(
                    "SELECT backfill_complete FROM ingest_state WHERE symbol='BTCUSDT'"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(collector.parse_since("2023-10-06T16:00:00Z"), 1696608000000)

    def test_unfinished_candle_is_never_stored(self):
        # Bybit's newest row is the minute that is still forming; it must not
        # reach the database, otherwise a backtest would trade on a partial close.
        closed = self.end - 60_000
        rows = collector.parse_rows(
            "BTCUSDT", [[str(self.end), "1", "1", "1", "1", "1", "1"]], self.end
        )
        self.assertEqual(rows, [])
        rows = collector.parse_rows(
            "BTCUSDT", [[str(closed), "1", "1", "1", "1", "1", "1"]], self.end
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], closed)


if __name__ == "__main__":
    unittest.main()
