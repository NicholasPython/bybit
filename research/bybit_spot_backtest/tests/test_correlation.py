"""Access boundary and snapshot integrity checks for the research page."""

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
os.environ["BYBIT_WEB_OWNER"] = "owner@example.test"
_jobs = tempfile.TemporaryDirectory()
os.environ["BYBIT_WEB_JOBS"] = _jobs.name
dashboard = importlib.import_module("app")


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.patch = mock.patch.object(dashboard, "RESEARCH_DIR", self.directory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        coins = [f"COIN{i}USDT" for i in range(20)]
        matrix = [
            [None if i == 19 or j == 19 else (1.0 if i == j else 0.5) for j in range(20)]
            for i in range(20)
        ]
        self.data = {
            "coins": coins,
            "ranking": [{"symbol": coin} for coin in coins],
            "matrices": {"30": matrix, "90": matrix},
            "snapshot": {},
            "analysis_end_ms": 0,
            "analysis_end_cst": "",
            "windows_cst": {},
            "method": "",
            "pairs": [],
            "common_short_window": {},
            "exploratory_clusters_90d": [],
            "independent_validation": {},
        }
        self.write()
        self.client = dashboard.app.test_client()
        self.owner = {"Tailscale-User-Login": "owner@example.test"}

    def write(self):
        (self.directory / "analysis.json").write_text(json.dumps(self.data), encoding="utf-8")

    def test_all_research_resources_require_owner(self):
        for url in (
            "/correlation",
            "/api/correlation",
            "/static/correlation.js",
            "/api/correlation/download/analysis.json",
        ):
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(
                self.client.get(
                    url, headers={"Tailscale-User-Login": "other@example.test"}
                ).status_code,
                403,
            )
        response = self.client.get("/correlation", headers=self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(b"/correlation", self.client.get("/", headers=self.owner).data)

    def test_missing_history_stays_null_and_order_is_preserved(self):
        response = self.client.get("/api/correlation", headers=self.owner)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["coins"], self.data["coins"])
        self.assertIsNone(data["matrices"]["90"][0][19])
        self.assertEqual(data["matrices"]["30"][0][1], 0.5)

    def test_missing_corrupt_or_inconsistent_snapshot_fails_closed(self):
        path = self.directory / "analysis.json"
        path.unlink()
        self.assertEqual(self.client.get("/api/correlation", headers=self.owner).status_code, 503)
        path.write_text('{"coins":NaN}')
        self.assertEqual(self.client.get("/api/correlation", headers=self.owner).status_code, 503)
        self.write()
        self.data["matrices"]["90"][0][1] = 2
        self.write()
        self.assertEqual(self.client.get("/api/correlation", headers=self.owner).status_code, 503)

    def test_downloads_are_attachments_and_cannot_choose_server_paths(self):
        response = self.client.get("/api/correlation/download/analysis.json", headers=self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        response.close()
        for name in ("app.py", "%2e%2e%2fapp.py", "top20_hourly.sqlite3", "unknown.csv"):
            self.assertEqual(
                self.client.get(
                    "/api/correlation/download/" + name, headers=self.owner
                ).status_code,
                404,
            )
        self.assertEqual(
            self.client.get("/api/correlation/download/report.md", headers=self.owner).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
