"""Regression checks for the mounted-under-a-prefix deployment.

The module is served at /spot-lab behind Tailscale Serve. Every asset and API
URL in the HTML must carry that prefix, otherwise the page loads unstyled and
the browser talks to the trading console instead of the research module.
"""

import importlib
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))
os.environ["BYBIT_WEB_OWNER"] = "owner@example.test"
_jobs = tempfile.TemporaryDirectory()
os.environ["BYBIT_WEB_JOBS"] = _jobs.name

PREFIX = "/spot-lab"
os.environ["BYBIT_WEB_PREFIX"] = PREFIX
# Load a dedicated copy of the app so the prefix is applied even when another
# test module has already imported the unprefixed application.
_spec = importlib.util.spec_from_file_location("app_prefixed", ROOT / "web" / "app.py")
dashboard = importlib.util.module_from_spec(_spec)
sys.modules["app_prefixed"] = dashboard
_spec.loader.exec_module(dashboard)


class PrefixDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_path = Path(self.temp.name) / "market.sqlite3"
        collector = importlib.import_module("collector")
        with collector.open_db(db_path) as db:
            collector.insert_rows(
                db,
                [
                    (
                        "BTCUSDT",
                        1_700_000_000_000,
                        "1",
                        "1",
                        "1",
                        "1",
                        "1",
                        "1",
                        1_700_000_000_000,
                    )
                ],
            )
        self.original_db = dashboard.DB_PATH
        dashboard.DB_PATH = db_path
        self.addCleanup(setattr, dashboard, "DB_PATH", self.original_db)
        self.client = dashboard.app.test_client()
        self.owner = {"Tailscale-User-Login": "owner@example.test"}

    def urls(self, body):
        return re.findall(rb'(?:href|src)="([^"]+)"', body)

    def test_page_links_carry_the_prefix(self):
        response = self.client.get(PREFIX + "/", headers=self.owner)
        self.assertEqual(response.status_code, 200)
        urls = [url.decode() for url in self.urls(response.data)]
        self.assertIn(PREFIX + "/static/style.css", urls)
        self.assertIn(PREFIX + "/static/app.js", urls)
        self.assertIn(PREFIX + "/correlation", urls)
        for url in urls:
            if url.startswith("/"):
                self.assertTrue(url.startswith(PREFIX + "/"), url)

    def test_frontend_receives_the_prefix(self):
        response = self.client.get(PREFIX + "/", headers=self.owner)
        self.assertIn(f'data-prefix="{PREFIX}"'.encode(), response.data)

    def test_prefixed_assets_and_endpoints_are_reachable(self):
        for path in (
            PREFIX + "/static/style.css",
            PREFIX + "/static/app.js",
            PREFIX + "/static/correlation.js",
            PREFIX + "/healthz",
        ):
            self.assertEqual(self.client.get(path, headers=self.owner).status_code, 200, path)

    def test_correlation_page_links_carry_the_prefix(self):
        response = self.client.get(PREFIX + "/correlation", headers=self.owner)
        self.assertEqual(response.status_code, 200)
        urls = [url.decode() for url in self.urls(response.data)]
        self.assertIn(PREFIX + "/static/correlation.css", urls)
        self.assertIn(PREFIX + "/static/correlation.js", urls)
        for url in urls:
            if url.startswith("/"):
                self.assertTrue(url.startswith(PREFIX + "/"), url)

    def test_api_routes_remain_reachable_under_the_prefix(self):
        self.assertEqual(self.client.get(PREFIX + "/api/status").status_code, 403)
        self.assertEqual(
            self.client.get(PREFIX + "/api/status", headers=self.owner).status_code, 200
        )


if __name__ == "__main__":
    unittest.main()
