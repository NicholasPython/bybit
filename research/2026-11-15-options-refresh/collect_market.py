#!/usr/bin/env python3
"""Read-only current public Bybit options and underlying market snapshot.

No credentials or private/account/order APIs. Every run writes downloaded JSON to
its own new directory under snapshots/, separate from these research scripts.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REMOTE = r"""
import concurrent.futures
import datetime as dt
import json
import time
import httpx

client = httpx.Client(proxy="http://127.0.0.1:7890", timeout=35)
records = []

def fetch(path, params):
    for attempt in range(4):
        try:
            response = client.get("https://api.bybit.com" + path, params=params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("retCode") != 0:
                raise ValueError(str(payload))
            return {"path": path, "params": params,
                    "receivedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "payload": payload}
        except Exception as exc:
            if attempt == 3:
                return {"path": path, "params": params,
                        "receivedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "error": str(exc)}
            time.sleep(attempt + 1)

started = dt.datetime.now(dt.timezone.utc).isoformat()
clock = fetch("/v5/market/time", {})
records.append(clock)
if "error" in clock:
    raise RuntimeError(clock["error"])
asof_ms = int(clock["payload"]["time"])
instruments = []
cursor = ""
seen = set()
while True:
    params = {"category": "option", "baseCoin": "All", "limit": 1000}
    if cursor:
        params["cursor"] = cursor
    record = fetch("/v5/market/instruments-info", params)
    records.append(record)
    if "error" in record:
        raise RuntimeError(record["error"])
    result = record["payload"]["result"]
    instruments.extend(result["list"])
    cursor = result.get("nextPageCursor", "")
    if not cursor:
        break
    if cursor in seen:
        raise RuntimeError("Repeated instruments cursor")
    seen.add(cursor)

bases = sorted({i["baseCoin"] for i in instruments if i["status"] == "Trading"})
crypto = [b for b in bases if b in {"BTC", "ETH", "SOL", "XAUT", "XRP", "HYPE", "MNT", "DOGE"}]
jobs = [("/v5/market/tickers", {"category": "option", "baseCoin": b}) for b in bases]
for base in crypto:
    symbol = base + "USDT"
    jobs.extend([
        ("/v5/market/tickers", {"category": "spot", "symbol": symbol}),
        ("/v5/market/tickers", {"category": "linear", "symbol": symbol}),
        ("/v5/market/kline", {"category": "spot", "symbol": symbol,
                               "interval": "60", "limit": 1000, "end": asof_ms}),
        ("/v5/market/kline", {"category": "spot", "symbol": symbol,
                               "interval": "D", "limit": 100, "end": asof_ms}),
    ])
for base in ("BTC", "ETH"):
    jobs.append(("/v5/market/funding/history", {
        "category": "linear", "symbol": base + "USDT", "limit": 100}))
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    records.extend(executor.map(lambda job: fetch(*job), jobs))
client.close()
print(json.dumps({"startedUtc": started,
                  "completedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "asOfMs": asof_ms, "bases": bases, "cryptoBases": crypto,
                  "instruments": instruments, "records": records}, separators=(",", ":")))
"""


def main():
    parent = ROOT / "snapshots"
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="market-", dir=parent))
    result = subprocess.run(
        [
            "/usr/bin/ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "nas",
            "podman exec -i bybitbot-worker python -I -",
        ],
        input=REMOTE,
        text=True,
        capture_output=True,
        timeout=300,
        check=True,
    )
    snapshot = json.loads(result.stdout)
    (output / "public_market.json").write_text(json.dumps(snapshot, separators=(",", ":")) + "\n")
    errors = [
        {"path": r["path"], "params": r["params"], "error": r["error"]}
        for r in snapshot["records"]
        if "error" in r
    ]
    print(
        json.dumps(
            {
                "snapshot": str(output / "public_market.json"),
                "startedUtc": snapshot["startedUtc"],
                "completedUtc": snapshot["completedUtc"],
                "bases": snapshot["bases"],
                "instruments": len(snapshot["instruments"]),
                "records": len(snapshot["records"]),
                "errors": errors,
            },
            indent=2,
        )
    )
    if errors:
        raise SystemExit("Snapshot contains failed public requests; inspect before use.")


if __name__ == "__main__":
    main()
