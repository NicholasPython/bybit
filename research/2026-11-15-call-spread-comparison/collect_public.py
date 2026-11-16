#!/usr/bin/env python3
"""One-shot BTC/ETH public Bybit data; only the explicit market GET allowlist.

Local curl ignores curlrc and proxies. No credentials, account or order routes.
Every response body is saved untouched in its own new empty directory, separate
from scripts. Run with Python -I. No daemon or scheduled task is created.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

ALLOWED = {
    "/v5/market/time",
    "/v5/market/instruments-info",
    "/v5/market/tickers",
    "/v5/market/kline",
}


def collect(output_parent):
    output_parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="market-", dir=output_parent))
    manifest = {
        "startedUtc": dt.datetime.now(dt.UTC).isoformat(),
        "transport": "local curl -q --noproxy '*'; public GET allowlist only",
        "records": [],
    }

    def get(path, params):
        assert path in ALLOWED
        assert params.get("category", "spot") in ("spot", "option")
        folder = Path(tempfile.mkdtemp(prefix="request-", dir=output))
        raw_file = folder / "response.json"
        started = dt.datetime.now(dt.UTC).isoformat()
        url = "https://api.bybit.com" + path + "?" + urllib.parse.urlencode(params)
        result = subprocess.run(  # noqa: S603 -- fixed curl binary and public GET URL allowlist
            [
                "/usr/bin/curl",
                "-q",
                "--noproxy",
                "*",
                "--max-time",
                "25",
                "--fail",
                "--silent",
                "--show-error",
                url,
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        received = dt.datetime.now(dt.UTC).isoformat()
        raw_file.write_bytes(result.stdout)
        record = {
            "path": path,
            "params": params,
            "startedUtc": started,
            "receivedUtc": received,
            "file": str(raw_file.relative_to(output)),
            "sha256": hashlib.sha256(result.stdout).hexdigest(),
        }
        manifest["records"].append(record)
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        payload = json.loads(result.stdout)
        assert payload.get("retCode") == 0, payload
        return payload

    clock = get("/v5/market/time", {})
    now = int(clock["time"])
    manifest["asOfMs"] = now
    for base in ("BTC", "ETH"):
        cursor = ""
        seen = set()
        while True:
            params = {"category": "option", "baseCoin": base, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = get("/v5/market/instruments-info", params)["result"]
            cursor = data.get("nextPageCursor", "")
            if not cursor:
                break
            assert cursor not in seen
            seen.add(cursor)
        get("/v5/market/tickers", {"category": "option", "baseCoin": base})
        get("/v5/market/tickers", {"category": "spot", "symbol": base + "USDT"})
        for interval, limit in (("60", 1000), ("D", 100)):
            get(
                "/v5/market/kline",
                {
                    "category": "spot",
                    "symbol": base + "USDT",
                    "interval": interval,
                    "limit": limit,
                    "end": now,
                },
            )
    manifest["completedUtc"] = dt.datetime.now(dt.UTC).isoformat()
    dest = output / "manifest.json"
    dest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "manifest": str(dest),
                "requests": len(manifest["records"]),
                "startedUtc": manifest["startedUtc"],
                "completedUtc": manifest["completedUtc"],
                "manifestSha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output_parent", type=Path)
    args = parser.parse_args()
    collect(args.output_parent)


if __name__ == "__main__":
    main()
