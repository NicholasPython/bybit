#!/usr/bin/env python3
"""Resumable Bybit public 1-minute spot candle collector. No API credentials.

Transport: local process -> ``ssh nas`` -> remote ``python3 -I -`` stdlib urllib.
One SSH connection is reused for the whole run and carries one JSON request
line per exchange. The local side validates every endpoint/parameter against a
fixed allowlist; the remote side only runs those validated GETs. Raw exchange
responses can be saved for audit, and every row is validated before it reaches
SQLite.

The exchange's ``end`` parameter is inclusive and returns up to 1000 candles
ending at that time, so backward paging sets the next ``end`` to the oldest
timestamp in the page. Forward sync uses ``end = now`` and stops as soon as a
page reaches already-confirmed bars.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

from web.market_config import INSTRUMENTS

BASE_URL = "https://api.bybit.com"
BAR_MS = 60_000
PAGE_LIMIT = 1000
DEFAULT_DB = Path(__file__).resolve().parent / "bybit_spot_1m.sqlite3"
DEFAULT_SSH_HOST = os.environ.get("BYBIT_COLLECTOR_SSH_HOST", "nas")
ALLOWED = {
    "/v5/market/time": frozenset(),
    "/v5/market/instruments-info": frozenset({"category", "limit", "cursor"}),
    "/v5/market/kline": frozenset({"category", "symbol", "interval", "limit", "start", "end"}),
    "/v5/market/tickers": frozenset({"category", "limit"}),
}


def utc_now_ms():
    return int(time.time() * 1000)


def utc_iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat() if ms else None


def _validate_request(path, params):
    if path not in ALLOWED or not isinstance(params, dict):
        raise ValueError("Public endpoint outside allowlist")
    if set(params) - ALLOWED[path]:
        raise ValueError("Unexpected public request parameter")
    if path == "/v5/market/instruments-info":
        if params.get("category") != "spot" or params.get("limit") != 1000:
            raise ValueError("Unexpected instruments parameters")
    elif path == "/v5/market/tickers":
        if params.get("category") != "spot" or params.get("limit") != 1000:
            raise ValueError("Unexpected tickers parameters")
    elif path == "/v5/market/kline":
        if params.get("category") != "spot" or params.get("interval") != "1":
            raise ValueError("Unexpected kline parameters")
        if params.get("symbol") not in INSTRUMENTS or params.get("limit") != 1000:
            raise ValueError("Unexpected kline symbol or limit")
        if not params.get("start") and not params.get("end"):
            raise ValueError("Kline request needs a window boundary")
    return dict(params)


# Executed on the remote host. It contains no credentials and no imports beyond
# the Python standard library; requests are validated locally first. The script
# speaks one JSON request line per exchange, so a single SSH connection can
# serve a whole backfill instead of one handshake per page.
_REMOTE = r"""
import datetime, json, sys, time, urllib.parse, urllib.request
while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    jobs = json.loads(line)
    records = []
    remote = None
    for path, params in jobs:
        url = "https://api.bybit.com" + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={"User-Agent": "curl/8.0", "Accept": "application/json"}
        )
        local = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    payload = json.load(response)
                if payload.get("retCode") != 0 or not isinstance(payload.get("result"), dict):
                    raise RuntimeError("Bybit API error " + str(payload.get("retCode")))
                local = payload
                break
            except Exception as error:
                remote = type(error).__name__ + ": " + str(error)
                time.sleep(0.4 * (attempt + 1))
        records.append({"path": path, "params": params,
                        "receivedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        **({"payload": local} if local is not None else {"error": remote})})
        time.sleep(0.05)
    sys.stdout.write(json.dumps({"records": records}, separators=(",", ":")) + "\n")
    sys.stdout.flush()
"""


class CandlePage(NamedTuple):
    """One kline page: the raw rows plus the exchange clock that produced them."""

    symbol: str
    end: int
    rows: list | None
    error: str | None
    server_ms: int


class BybitPublic:
    """Allowlisted public GET transport over SSH; no secrets or account access.

    The SSH connection stays open for the whole run. Each handshake costs
    several seconds against the remote host, which would otherwise dominate a
    multi-year backfill that needs thousands of pages.
    """

    def __init__(self, ssh_host=DEFAULT_SSH_HOST, interval=0.05, timeout=90):
        self.ssh_host = ssh_host
        self.interval = interval
        self.timeout = timeout
        self.next_request = 0.0
        self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _start(self):
        # ssh joins arguments into a shell command, so the fixed remote script
        # is base64-encoded and decoded there; the validated jobs go over stdin.
        encoded = base64.b64encode(_REMOTE.encode()).decode("ascii")
        remote_cmd = f"python3 -I -c \"import base64;exec(base64.b64decode('{encoded}'))\""
        self.process = subprocess.Popen(  # noqa: S603 -- fixed ssh command, validated public GET allowlist
            [
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                self.ssh_host,
                remote_cmd,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        with contextlib.suppress(OSError):
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def _readline(self):
        deadline = time.monotonic() + self.timeout
        buffer = b""
        while not buffer.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("Public transport timed out")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise OSError("Public transport timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise OSError("Public transport closed")
            buffer += chunk
        return buffer

    def _exchange(self, line):
        """Send one request line and read one answer line, reconnecting once if the link drops."""
        last = None
        for _attempt in range(2):
            try:
                if self.process is None:
                    self._start()
                self.process.stdin.write(line.encode("utf-8") + b"\n")
                self.process.stdin.flush()
                return self._readline()
            except (OSError, ValueError) as exc:
                last = exc
                self.close()
        raise RuntimeError(f"Public transport failed: {last}") from last

    def get_many(self, jobs):
        """Run every validated GET in one exchange with the remote host.

        A per-job failure is handed back to the caller instead of aborting the
        batch, so one bad symbol cannot discard the other nineteen.
        """
        prepared = [(path, _validate_request(path, params)) for path, params in jobs]
        if not prepared:
            raise ValueError("No public requests")
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + self.interval * len(prepared)
        line = json.dumps(prepared, separators=(",", ":"))
        envelope = json.loads(self._exchange(line))
        if not isinstance(envelope.get("records"), list) or len(envelope["records"]) != len(
            prepared
        ):
            raise RuntimeError("Malformed public response envelope")
        return envelope["records"]

    def get(self, path, params):
        record = self.get_many([(path, params)])[0]
        if "error" in record:
            raise RuntimeError(record["error"])
        if record.get("payload", {}).get("retCode") != 0:
            raise RuntimeError("Public API failure")
        return record["payload"]

    def candles(self, symbol, end=None, start=None, limit=PAGE_LIMIT):
        params = {"category": "spot", "symbol": symbol, "interval": "1", "limit": limit}
        params["end"] = int(end) if end is not None else utc_now_ms()
        if start is not None:
            params["start"] = int(start)
        payload = self.get("/v5/market/kline", params)
        rows = payload["result"].get("list")
        if not isinstance(rows, list):
            raise RuntimeError("Invalid kline response")
        return rows

    def candle_pages(self, requests):
        """Fetch one kline page per ``(symbol, end)`` request in one round trip.

        Batching the universe into a single SSH hop is what makes a multi-year
        backfill finish in minutes instead of hours. Every response carries the
        exchange's own clock, which is what decides whether a bar has closed.
        """
        prepared = [
            (symbol, int(end) if end is not None else utc_now_ms()) for symbol, end in requests
        ]
        jobs = [
            (
                "/v5/market/kline",
                {
                    "category": "spot",
                    "symbol": symbol,
                    "interval": "1",
                    "limit": PAGE_LIMIT,
                    "end": end,
                },
            )
            for symbol, end in prepared
        ]
        pages = []
        for (symbol, end), record in zip(prepared, self.get_many(jobs), strict=True):
            if "error" in record:
                pages.append(CandlePage(symbol, end, None, str(record["error"]), 0))
                continue
            payload = record.get("payload") or {}
            if payload.get("retCode") != 0:
                pages.append(
                    CandlePage(symbol, end, None, f"Bybit API error {payload.get('retCode')}", 0)
                )
                continue
            rows = payload["result"].get("list")
            if not isinstance(rows, list):
                pages.append(CandlePage(symbol, end, None, "Invalid kline response", 0))
                continue
            try:
                server_ms = int(payload.get("time") or 0)
            except (TypeError, ValueError):
                server_ms = 0
            pages.append(CandlePage(symbol, end, rows, None, server_ms or utc_now_ms()))
        return pages


def open_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS instruments (
            symbol TEXT PRIMARY KEY,
            base_coin TEXT NOT NULL,
            quote_coin TEXT NOT NULL,
            state TEXT NOT NULL,
            list_time_ms INTEGER,
            checked_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candles_1m (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            o TEXT NOT NULL,
            h TEXT NOT NULL,
            l TEXT NOT NULL,
            c TEXT NOT NULL,
            vol TEXT NOT NULL,
            turnover TEXT NOT NULL,
            received_at_ms INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts_ms)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS ingest_state (
            symbol TEXT PRIMARY KEY,
            backfill_cursor_ms INTEGER,
            backfill_complete INTEGER NOT NULL DEFAULT 0,
            latest_sync_ms INTEGER,
            last_error TEXT,
            updated_at_ms INTEGER NOT NULL
        );
    """)
    return db


def refresh_instruments(db, client):
    wanted = set(INSTRUMENTS)
    found = {}
    cursor = ""
    seen_cursors = set()
    while True:
        params = {"category": "spot", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = client.get("/v5/market/instruments-info", params)
        rows = payload["result"].get("list")
        if not isinstance(rows, list):
            raise RuntimeError("Invalid instruments response")
        for item in rows:
            symbol = item.get("symbol")
            if symbol in wanted:
                found[symbol] = item
        cursor = payload["result"].get("nextPageCursor", "")
        if not cursor or cursor in seen_cursors:
            if cursor in seen_cursors:
                raise RuntimeError("Repeated instrument cursor")
            break
        seen_cursors.add(cursor)
    now = utc_now_ms()
    with db:
        for symbol in INSTRUMENTS:
            item = found.get(symbol)
            if item is None:
                exists = db.execute(
                    "SELECT 1 FROM instruments WHERE symbol=?", (symbol,)
                ).fetchone()
                if exists is None:
                    raise RuntimeError(f"Missing Bybit spot instrument: {symbol}")
                db.execute(
                    "UPDATE instruments SET state='not_returned', checked_at_ms=? WHERE symbol=?",
                    (now, symbol),
                )
                continue
            db.execute(
                "INSERT INTO instruments VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET state=excluded.state, "
                "list_time_ms=excluded.list_time_ms, checked_at_ms=excluded.checked_at_ms",
                (
                    symbol,
                    item.get("baseCoin", ""),
                    item.get("quoteCoin", ""),
                    item.get("status", ""),
                    int(item.get("listTime") or 0) or None,
                    now,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO ingest_state(symbol,updated_at_ms) VALUES (?,?)",
                (symbol, now),
            )


def parse_rows(symbol, raw_rows, now_ms=None):
    """Validate Bybit ``[ts, open, high, low, close, volume, turnover]`` rows.

    The newest row Bybit returns is the minute that is still forming. A bar
    whose close time has not passed yet is dropped: a partial close must never
    reach a backtest, and the next sync stores the finished bar instead.
    """
    reference = utc_now_ms() if now_ms is None else int(now_ms)
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) != 7:
            raise RuntimeError(f"Malformed candle for {symbol}: {raw!r}")
        ts = int(raw[0])
        if ts % BAR_MS:
            raise RuntimeError(f"Misaligned candle timestamp for {symbol}: {ts}")
        if ts + BAR_MS > reference:
            continue
        if any(len(str(value)) > 64 for value in raw[1:]):
            raise RuntimeError(f"Candle numeric value too long for {symbol} at {ts}")
        try:
            values = [Decimal(str(value)) for value in raw[1:]]
        except (InvalidOperation, TypeError) as exc:
            raise RuntimeError(f"Invalid candle numeric value for {symbol} at {ts}") from exc
        if any(not value.is_finite() or value < 0 for value in values) or any(
            value <= 0 for value in values[:4]
        ):
            raise RuntimeError(f"Invalid candle price/volume for {symbol} at {ts}")
        opened, high, low, closed = values[:4]
        if high < max(opened, closed) or low > min(opened, closed) or high < low:
            raise RuntimeError(f"Invalid OHLC order for {symbol} at {ts}")
        rows.append((symbol, ts, raw[1], raw[2], raw[3], raw[4], raw[5], raw[6], utc_now_ms()))
    return rows


def insert_rows(db, rows):
    if rows:
        with db:
            db.executemany(
                "INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,ts_ms) DO UPDATE SET "
                "o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,"
                "vol=excluded.vol,turnover=excluded.turnover,"
                "received_at_ms=excluded.received_at_ms",
                rows,
            )


def set_state(db, symbol, **fields):
    fields["updated_at_ms"] = utc_now_ms()
    assignments = ", ".join(f"{key}=?" for key in fields)
    with db:
        db.execute(
            f"UPDATE ingest_state SET {assignments} WHERE symbol=?",  # noqa: S608 -- fixed column names
            (*fields.values(), symbol),
        )


def _page_bounds(raw_rows):
    stamps = [int(row[0]) for row in raw_rows]
    return max(stamps), min(stamps)


def sync_latest(db, client):
    """Bring every universe symbol up to date with the newest closed candles.

    A fresh database has nothing confirmed yet, so one page of the most recent
    candles is enough here; walking further back is ``backfill``'s job.
    """
    refresh_instruments(db, client)
    failures = set()
    cursors = {}
    for symbol in INSTRUMENTS:
        state = db.execute("SELECT state FROM instruments WHERE symbol=?", (symbol,)).fetchone()
        if state is None or state[0] != "Trading":
            print(f"sync skipped {symbol}: state={state[0] if state else 'missing'}", flush=True)
            continue
        previous_max = db.execute(
            "SELECT MAX(ts_ms) FROM candles_1m WHERE symbol=?", (symbol,)
        ).fetchone()[0]
        cursors[symbol] = {"cursor": None, "previous_max": previous_max, "total": 0}
    active = list(cursors)
    while active:
        pages = client.candle_pages([(symbol, cursors[symbol]["cursor"]) for symbol in active])
        pending = []
        for page in pages:
            info = cursors[page.symbol]
            if page.error is not None:
                set_state(db, page.symbol, last_error=page.error)
                failures.add(page.symbol)
                continue
            if not page.rows:
                set_state(db, page.symbol, latest_sync_ms=utc_now_ms(), last_error=None)
                continue
            newest, oldest = _page_bounds(page.rows)
            rows = parse_rows(page.symbol, page.rows, page.server_ms)
            insert_rows(db, rows)
            info["total"] += len(rows)
            if info["previous_max"] is None or oldest <= info["previous_max"]:
                set_state(db, page.symbol, latest_sync_ms=utc_now_ms(), last_error=None)
                latest = db.execute(
                    "SELECT MAX(ts_ms) FROM candles_1m WHERE symbol=?", (page.symbol,)
                ).fetchone()[0]
                print(
                    f"sync {page.symbol}: fetched={info['total']} confirmed candles, "
                    f"latest={utc_iso(latest)}",
                    flush=True,
                )
                continue
            if info["cursor"] is not None and oldest >= info["cursor"]:
                set_state(
                    db, page.symbol, last_error=f"Pagination did not advance for {page.symbol}"
                )
                failures.add(page.symbol)
                continue
            info["cursor"] = oldest - BAR_MS  # exchange end is inclusive
            pending.append(page.symbol)
        active = pending
    if failures:
        raise RuntimeError("Sync failed for: " + ", ".join(sorted(failures)))


def backfill(db, client, max_pages, since_ms=None):
    """Walk every symbol backwards until the target date or the exchange's end."""
    if all(
        db.execute("SELECT backfill_complete FROM ingest_state WHERE symbol=?", (s,)).fetchone()
        == (1,)
        for s in INSTRUMENTS
    ):
        print("backfill already complete", flush=True)
        return
    refresh_instruments(db, client)
    pages = 0
    failures = set()
    cursors = {}
    for symbol in INSTRUMENTS:
        state = db.execute(
            "SELECT backfill_cursor_ms,backfill_complete FROM ingest_state WHERE symbol=?",
            (symbol,),
        ).fetchone()
        cursor, complete = state if state else (None, 0)
        if complete:
            continue
        if since_ms is not None and cursor is not None and cursor <= since_ms:
            set_state(db, symbol, backfill_complete=1, last_error=None)
            print(f"backfill target reached {symbol}: {utc_iso(cursor)}", flush=True)
            continue
        cursors[symbol] = cursor
    while cursors and pages < max_pages:
        batch = []
        for symbol in cursors:
            if pages + len(batch) >= max_pages:
                break
            batch.append(symbol)
        oldest = None
        for page in client.candle_pages([(symbol, cursors[symbol]) for symbol in batch]):
            pages += 1
            symbol = page.symbol
            if page.error is not None:
                set_state(db, symbol, last_error=page.error)
                failures.add(symbol)
                cursors.pop(symbol, None)
                continue
            if not page.rows:
                set_state(db, symbol, backfill_complete=1, last_error=None)
                print(f"backfill complete {symbol}", flush=True)
                cursors.pop(symbol, None)
                continue
            newest, page_oldest = _page_bounds(page.rows)
            cursor = cursors[symbol]
            if cursor is not None and page_oldest >= cursor:
                set_state(db, symbol, last_error=f"Pagination did not advance for {symbol}")
                failures.add(symbol)
                cursors.pop(symbol, None)
                continue
            rows = parse_rows(symbol, page.rows, page.server_ms)
            if since_ms is not None:
                rows = [row for row in rows if row[1] >= since_ms]
            insert_rows(db, rows)
            reached_target = since_ms is not None and page_oldest <= since_ms
            set_state(
                db,
                symbol,
                backfill_cursor_ms=page_oldest - BAR_MS,
                backfill_complete=int(reached_target),
                last_error=None,
            )
            if reached_target:
                print(f"backfill target reached {symbol}: {utc_iso(since_ms)}", flush=True)
                cursors.pop(symbol, None)
            else:
                cursors[symbol] = page_oldest - BAR_MS
            oldest = page_oldest
        if oldest is not None and pages % 80 < len(batch):
            print(f"backfill pages={pages} oldest={utc_iso(oldest)}", flush=True)
    print(f"backfill finished this run: pages={pages}", flush=True)
    if failures:
        raise RuntimeError("Backfill failed for: " + ", ".join(sorted(failures)))


def first_gap(db, symbol):
    count, first, last = db.execute(
        "SELECT COUNT(*),MIN(ts_ms),MAX(ts_ms) FROM candles_1m WHERE symbol=?", (symbol,)
    ).fetchone()
    if count < 2 or (last - first) // BAR_MS + 1 == count:
        return None
    previous = None
    for (ts,) in db.execute(
        "SELECT ts_ms FROM candles_1m WHERE symbol=? ORDER BY ts_ms", (symbol,)
    ):
        if previous is not None and ts > previous + BAR_MS:
            return previous + BAR_MS, ts
        previous = ts
    return None


def repair_gaps(db, client, max_pages):
    pages = 0
    failures = []
    for symbol in INSTRUMENTS:
        while pages < max_pages:
            gap = first_gap(db, symbol)
            if gap is None:
                break
            gap_start, gap_end = gap
            # ``end`` is inclusive, so the newest missing minute is the cursor
            # itself; keep walking back until the cursor passes the gap start.
            cursor = gap_end - BAR_MS
            while cursor >= gap_start and pages < max_pages:
                page = client.candle_pages([(symbol, cursor)])[0]
                pages += 1
                if page.error is not None:
                    raise RuntimeError(page.error)
                if not page.rows:
                    break
                newest, oldest = _page_bounds(page.rows)
                if oldest - BAR_MS >= cursor:
                    raise RuntimeError(f"Repair pagination did not advance for {symbol}")
                insert_rows(db, parse_rows(symbol, page.rows, page.server_ms))
                cursor = oldest - BAR_MS
            if first_gap(db, symbol) == gap:
                failures.append(f"{symbol} {utc_iso(gap_start)}")
                break
        if pages >= max_pages:
            break
    print(f"repair finished this run: pages={pages}", flush=True)
    if failures:
        raise RuntimeError("Unrepaired gaps: " + ", ".join(failures))


def status(db):
    for symbol in INSTRUMENTS:
        count, first, last = db.execute(
            "SELECT COUNT(*),MIN(ts_ms),MAX(ts_ms) FROM candles_1m WHERE symbol=?", (symbol,)
        ).fetchone()
        state = db.execute(
            "SELECT backfill_cursor_ms,backfill_complete,latest_sync_ms,last_error "
            "FROM ingest_state WHERE symbol=?",
            (symbol,),
        ).fetchone()
        expected = (last - first) // BAR_MS + 1 if count else 0
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "rows": count,
                    "first_utc": utc_iso(first),
                    "last_utc": utc_iso(last),
                    "missing_minutes_within_range": expected - count,
                    "backfill_cursor_utc": utc_iso(state[0]) if state else None,
                    "backfill_complete": bool(state[1]) if state else False,
                    "latest_sync_utc": utc_iso(state[2]) if state else None,
                    "last_error": state[3] if state else None,
                },
                ensure_ascii=False,
            )
        )


def parse_since(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        timestamp_ms = int(parsed.timestamp() * 1000)
        if timestamp_ms <= 0 or timestamp_ms > utc_now_ms() or timestamp_ms % BAR_MS:
            raise ValueError("timestamp must be a past whole minute")
        return timestamp_ms
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            "--since must be a past ISO 8601 whole minute with timezone"
        ) from exc


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "sync", "backfill", "repair", "status"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--max-pages", type=int, default=800)
    parser.add_argument("--since", type=parse_since)
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if args.since is not None and args.command != "backfill":
        parser.error("--since is only available with backfill")
    db = open_db(args.db)
    client = None
    try:
        if args.command == "status":
            status(db)
        else:
            client = BybitPublic(ssh_host=args.ssh_host)
            if args.command == "init":
                refresh_instruments(db, client)
                status(db)
            elif args.command == "sync":
                sync_latest(db, client)
            elif args.command == "backfill":
                backfill(db, client, args.max_pages, args.since)
            elif args.command == "repair":
                repair_gaps(db, client, args.max_pages)
    finally:
        if client is not None:
            client.close()
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
