#!/usr/bin/env python3
"""Read-only public collector and deterministic research paper ledger.

No private endpoints, credentials, exchange orders, or production imports.
Run with Python -I. Downloaded JSON stays in separate new directories.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import subprocess
import tempfile
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
D = Decimal
ZERO = D("0")
HOUR = 3_600_000
DAY = 24 * HOUR
DONE = {"closed", "cancelled", "aborted", "expired_unresolved"}
OPEN_PURPOSES = {"open_long", "open_short"}
REMOTE = r"""
import concurrent.futures
import datetime as dt
import json
import httpx
jobs = __JOBS__
def fetch(job):
    path, params = job
    try:
        with httpx.Client(proxy="http://127.0.0.1:7890", timeout=25) as client:
            response = client.get("https://api.bybit.com" + path, params=params)
            response.raise_for_status()
            payload = response.json()
        if payload.get("retCode") != 0:
            raise ValueError(str(payload))
        return {"path": path, "params": params,
                "receivedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "payload": payload}
    except Exception as error:
        return {"path": path, "params": params,
                "receivedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "error": type(error).__name__ + ": " + str(error)}
started = dt.datetime.now(dt.timezone.utc).isoformat()
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    records = list(executor.map(fetch, jobs))
print(json.dumps({"startedUtc": started,
                  "completedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "records": records}, separators=(",", ":")))
"""


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def stamp(text):
    return int(dt.datetime.fromisoformat(text).timestamp() * 1000)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dec(record, key):
    value = D(str(record[key]))
    if not value.is_finite():
        raise ValueError(f"Nonfinite {key}")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        temporary = Path(file.name)
    temporary.replace(path)


def load_protocol(path=ROOT / "protocol.json"):
    protocol = json.loads(path.read_text())
    return protocol, digest(path)


def new_state(protocol, protocol_hash):
    return {
        "protocolHash": protocol_hash,
        "protocolVersion": protocol["version"],
        "simulation": True,
        "marginValidated": False,
        "lastMs": None,
        "startedMs": None,
        "processed": {},
        "events": [],
        "samples": [],
        "positions": {
            spec["base"]: {
                **spec,
                "phase": "waiting",
                "cash": "0",
                "fees": "0",
                "longHeld": False,
                "shortHeld": False,
                "pending": None,
            }
            for spec in protocol["positions"]
        },
    }


class IntegrityError(ValueError):
    """A frozen run cannot continue with changed inputs or checkpoint."""


def state_hash(state):
    content = {k: v for k, v in state.items() if k != "checkpointHash"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def prepare_run(output, protocol_hash):
    manifest_path = output / "manifest.json"
    engine_hash = digest(Path(__file__))
    if digest(ROOT / "protocol.json") != protocol_hash:
        raise IntegrityError("Protocol file changed during observation")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["protocolHash"] != protocol_hash or manifest["engineHash"] != engine_hash:
            raise IntegrityError("Frozen protocol or engine changed: create a new study")
    else:
        if (output / "state.json").exists():
            raise IntegrityError("Run manifest missing")
        manifest = {
            "protocolHash": protocol_hash,
            "engineHash": engine_hash,
            "frozenUtc": dt.datetime.now(dt.UTC).isoformat(),
        }
        atomic_json(manifest_path, manifest)
        (output / "protocol.json").write_bytes((ROOT / "protocol.json").read_bytes())
        (output / "PROTOCOL.md").write_bytes((ROOT / "PROTOCOL.md").read_bytes())
    return manifest


def collect(protocol, directory):
    """Fetch a fresh public quote snapshot into its own new directory."""
    directory.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="quote-", dir=directory))
    jobs = [("/v5/market/time", {})]
    for spec in protocol["positions"]:
        base = spec["base"]
        jobs.extend(
            [
                ("/v5/market/tickers", {"category": "option", "baseCoin": base}),
                (
                    "/v5/market/kline",
                    {
                        "category": "spot",
                        "symbol": base + "USDT",
                        "interval": "60",
                        "limit": 60,
                    },
                ),
                (
                    "/v5/market/kline",
                    {
                        "category": "spot",
                        "symbol": base + "USDT",
                        "interval": "D",
                        "limit": 70,
                    },
                ),
            ]
        )
        for symbol in (spec["long_symbol"], spec["short_symbol"]):
            jobs.append(("/v5/market/instruments-info", {"category": "option", "symbol": symbol}))
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
        input=REMOTE.replace("__JOBS__", json.dumps(jobs)),
        text=True,
        capture_output=True,
        check=True,
        timeout=150,
    )
    payload = json.loads(result.stdout)
    path = output / "public.json"
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    return path


def view(snapshot, protocol):
    """Turn a raw snapshot into a validated market view. Absent quotes stay absent."""
    records = snapshot["records"]
    completed_ms = stamp(snapshot["completedUtc"])
    max_age_ms = protocol["max_response_age_seconds"] * 1000
    times = []
    for r in records:
        if "error" in r:
            raise ValueError(f"Public collection failed: {r['error']}")
        payload = r["payload"]
        if payload.get("retCode") != 0:
            raise ValueError("Public API failure")
        source_ms = int(payload["time"])
        if not 0 <= stamp(r["receivedUtc"]) - source_ms <= max_age_ms:
            raise ValueError("Stale or future response at receipt")
        if not 0 <= completed_ms - source_ms <= max_age_ms:
            raise ValueError("Snapshot response skew exceeds bound")
        times.append(source_ms)
    market = {"ms": max(times), "quotes": {}, "instruments": {}, "signals": {}}
    for spec in protocol["positions"]:
        base = spec["base"]
        tickers = [
            r
            for r in records
            if r["path"] == "/v5/market/tickers" and r["params"].get("baseCoin") == base
        ]
        if len(tickers) != 1:
            raise ValueError("Missing or duplicate tickers response")
        ticker_record = tickers[0]
        for symbol in (spec["long_symbol"], spec["short_symbol"]):
            found = [t for t in ticker_record["payload"]["result"]["list"] if t["symbol"] == symbol]
            ins_records = [
                r
                for r in records
                if r["path"] == "/v5/market/instruments-info"
                and r["params"].get("symbol") == symbol
            ]
            if len(ins_records) != 1:
                raise ValueError("Missing instrument response")
            ins = [i for i in ins_records[0]["payload"]["result"]["list"] if i["symbol"] == symbol]
            if len(ins) != 1:
                raise ValueError("Missing instrument")
            market["instruments"][symbol] = ins[0]
            # A missing quote is recorded as unavailable, never as a zero valuation.
            if len(found) == 1:
                market["quotes"][symbol] = {
                    **found[0],
                    "sourceMs": int(ticker_record["payload"]["time"]),
                }
        closes = {}
        for interval, width in (("60", HOUR), ("D", DAY)):
            matches = [
                r
                for r in records
                if r["path"] == "/v5/market/kline"
                and r["params"].get("symbol") == base + "USDT"
                and r["params"].get("interval") == interval
            ]
            if len(matches) != 1:
                raise ValueError("Missing candle response")
            response = matches[0]
            cutoff = int(response["payload"]["time"])
            rows = sorted(
                [r for r in response["payload"]["result"]["list"] if int(r[0]) + width <= cutoff],
                key=lambda r: int(r[0]),
            )
            if not rows:
                raise ValueError("No completed candles")
            if any(int(r[0]) % width != 0 for r in rows):
                raise ValueError("Misaligned candle")
            if any(
                int(b[0]) - int(a[0]) != width for a, b in zip(rows[:-1], rows[1:], strict=True)
            ):
                raise ValueError("Candle gap or duplicate")
            for row in rows:
                o, hi, lo, c, volume, turnover = (D(v) for v in row[1:7])
                if (
                    not all(v.is_finite() for v in (o, hi, lo, c, volume, turnover))
                    or not 0 < lo <= min(o, c) <= max(o, c) <= hi
                    or min(volume, turnover) < 0
                ):
                    raise ValueError("Invalid completed OHLC")
            if not 0 <= cutoff - int(rows[-1][0]) - width < width:
                raise ValueError("Stale completed candles")
            closes[interval] = [D(r[4]) for r in rows]
        hours, daily = closes["60"], closes["D"]
        if len(hours) < 25 or len(daily) < 50:
            raise ValueError("Insufficient completed history")
        sma20, sma50 = sum(daily[-20:]) / 20, sum(daily[-50:]) / 50
        market["signals"][base] = {
            "eligible": hours[-1] < sma20 and hours[-1] < sma50 and hours[-1] < hours[-25],
            "hourClose": str(hours[-1]),
            "sma20": str(sma20),
            "sma50": str(sma50),
            "return24h": str(hours[-1] / hours[-25] - 1),
            "dailyBoundary": "exchange UTC day candles, unfinished day excluded",
        }
    return market


def price(market, symbol, side, protocol, quantity, spread_gate=False):
    """Conservative executable-side assumption requiring two-sided quotes and size."""
    q = market["quotes"].get(symbol)
    i = market["instruments"].get(symbol)
    if q is None or i is None:
        return None
    try:
        bid, ask = dec(q, "bid1Price"), dec(q, "ask1Price")
        if bid <= 0 or ask < bid:
            return None
        if min(dec(q, "bid1Size"), dec(q, "ask1Size")) < quantity:
            return None
        if spread_gate and (ask - bid) / ((ask + bid) / 2) > D(protocol["max_spread_fraction"]):
            return None
        tick = dec(i["priceFilter"], "tickSize")
        if tick <= 0 or dec(q, "indexPrice") <= 0 or bid % tick or ask % tick:
            return None
        slip = tick * protocol["adverse_slippage_ticks"]
        px = ask + slip if side == "Buy" else bid - slip
        if px < dec(i["priceFilter"], "minPrice") or px > dec(i["priceFilter"], "maxPrice"):
            return None
        return px if px > 0 else None
    except (KeyError, ArithmeticError, ValueError):
        return None


def spread_pct(market, symbol):
    q = market["quotes"].get(symbol)
    if q is None:
        return None
    try:
        bid, ask = dec(q, "bid1Price"), dec(q, "ask1Price")
        if bid <= 0 or ask < bid:
            return None
        return (ask - bid) / ((ask + bid) / 2)
    except (KeyError, ArithmeticError, ValueError):
        return None


def fee(market, symbol, px, qty, protocol):
    index = dec(market["quotes"][symbol], "indexPrice")
    return min(D(protocol["taker_rate"]) * index, D(protocol["premium_fee_cap"]) * px) * qty


def log(state, ms, kind, **fields):
    state["events"].append({"utc": iso(ms), "ms": ms, "kind": kind, **fields})


def intent(state, position, market, ms, purpose, symbol, side, protocol, limit=None):
    """Record a paper intent. It may only fill on a later, newer quote."""
    quote = market["quotes"].get(symbol)
    decision_source_ms = int(quote["sourceMs"]) if quote else ms
    position["pending"] = {
        "purpose": purpose,
        "symbol": symbol,
        "side": side,
        "createdMs": ms,
        "decisionSourceMs": decision_source_ms,
        # Entry intents expire; exit intents keep working until the contract expires.
        "deadlineMs": (
            ms + protocol["pending_ttl_seconds"] * 1000 if purpose in OPEN_PURPOSES else None
        ),
        "limit": str(limit) if limit is not None else None,
    }
    log(state, ms, "intent", base=position["base"], **position["pending"])


def contract_ok(market, position):
    qty = D(position["quantity"])
    long = market["instruments"].get(position["long_symbol"])
    short = market["instruments"].get(position["short_symbol"])
    if long is None or short is None:
        return False
    if (
        long.get("optionsType") != "Put"
        or short.get("optionsType") != "Put"
        or long.get("deliveryTime") != short.get("deliveryTime")
        or long.get("baseCoin") != position["base"]
        or short.get("baseCoin") != position["base"]
    ):
        return False
    position["expiryMs"] = int(long["deliveryTime"])
    # Instrument API has no strike field; Bybit suffix is expiry-strike-side-USDT.
    k1 = D(position["long_symbol"].rsplit("-", 3)[1])
    k2 = D(position["short_symbol"].rsplit("-", 3)[1])
    if k1 <= k2:
        return False
    position["widthValue"] = str((k1 - k2) * qty)
    for i in (long, short):
        lot = i.get("lotSizeFilter") or {}
        if (
            i.get("status") != "Trading"
            or i.get("quoteCoin") != "USDT"
            or i.get("settleCoin") != "USDT"
            or not lot
            or qty < dec(lot, "minOrderQty")
            or dec(lot, "qtyStep") <= 0
            or qty % dec(lot, "qtyStep") != 0
        ):
            return False
    return True


def value(position, market, protocol):
    """Mark the open position to an executable exit; unknown when a quote is missing."""
    if position["phase"] == "expired_unresolved":
        return None
    qty = D(position["quantity"])
    liquidation = ZERO
    for held, symbol, side, sign in (
        (position["longHeld"], position["long_symbol"], "Sell", 1),
        (position["shortHeld"], position["short_symbol"], "Buy", -1),
    ):
        if not held:
            continue
        px = price(market, symbol, side, protocol, qty)
        if px is None:
            return None
        liquidation += sign * px * qty - fee(market, symbol, px, qty, protocol)
    return {
        "cash": position["cash"],
        "liquidationValue": str(liquidation),
        "netPnl": str(D(position["cash"]) + liquidation),
    }


def limit_price(px, tick, fraction, side):
    value = px * (1 + fraction if side == "Buy" else 1 - fraction)
    rounding = ROUND_CEILING if side == "Buy" else ROUND_FLOOR
    return (value / tick).to_integral_value(rounding=rounding) * tick


def fill(position, market, pending, px, protocol, state):
    qty, symbol, side = D(position["quantity"]), pending["symbol"], pending["side"]
    cost = fee(market, symbol, px, qty, protocol)
    flow = (-1 if side == "Buy" else 1) * px * qty - cost
    position["cash"] = str(D(position["cash"]) + flow)
    position["fees"] = str(D(position["fees"]) + cost)
    purpose = pending["purpose"]
    if symbol == position["long_symbol"]:
        position["longHeld"] = purpose != "close_long"
    if symbol == position["short_symbol"]:
        position["shortHeld"] = purpose != "close_short"
    position["pending"] = None
    log(
        state,
        market["ms"],
        "simulated_fill",
        base=position["base"],
        purpose=purpose,
        symbol=symbol,
        side=side,
        quantity=str(qty),
        price=str(px),
        fee=str(cost),
        cashFlow=str(flow),
        quoteSourceMs=market["quotes"][symbol]["sourceMs"],
    )


def decide_entry(state, position, market, protocol):
    """Create the long-leg intent from the first qualifying snapshot."""
    ms, base, qty = market["ms"], position["base"], D(position["quantity"])
    expiry = position["expiryMs"]
    days = (expiry - ms) / DAY
    if days < protocol["min_entry_days"]:
        position["phase"] = "cancelled"
        log(state, ms, "entry_window_ended", base=base, daysRemaining=days)
        return
    if days > protocol["max_entry_days"]:
        return
    if not market["signals"][base]["eligible"]:
        return
    long_symbol, short_symbol = position["long_symbol"], position["short_symbol"]
    for symbol in (long_symbol, short_symbol):
        pct = spread_pct(market, symbol)
        if pct is not None and pct > D(protocol["max_spread_fraction"]):
            log(state, ms, "entry_spread_rejected", base=base, symbol=symbol, spreadPct=str(pct))
            return
    lp = price(market, long_symbol, "Buy", protocol, qty, True)
    sp = price(market, short_symbol, "Sell", protocol, qty, True)
    if lp is None or sp is None:
        log(state, ms, "entry_quote_unavailable", base=base)
        return
    debit = (lp - sp) * qty + fee(market, long_symbol, lp, qty, protocol)
    debit += fee(market, short_symbol, sp, qty, protocol)
    if (
        not ZERO < debit < D(position["widthValue"])
        or debit > D(protocol["max_debit_per_trade_usdt"])
        or lp * qty > D(protocol["max_long_premium_per_trade_usdt"])
    ):
        log(state, ms, "entry_budget_rejected", base=base, estimatedDebit=str(debit))
        return
    position["signal"] = market["signals"][base]
    position["decisionMs"] = ms
    for key, symbol, side in (
        ("longLimit", long_symbol, "Buy"),
        ("shortLimit", short_symbol, "Sell"),
    ):
        q = market["quotes"][symbol]
        raw = dec(q, "ask1Price" if side == "Buy" else "bid1Price")
        tick = dec(market["instruments"][symbol]["priceFilter"], "tickSize")
        position[key] = str(limit_price(raw, tick, D(protocol["entry_limit_fraction"]), side))
    position["phase"] = "opening_long"
    intent(
        state,
        position,
        market,
        ms,
        "open_long",
        long_symbol,
        "Buy",
        protocol,
        D(position["longLimit"]),
    )


def abort_entry(state, position, market, protocol, reason):
    """Cancel the short leg and unwind the long leg that is already paid for."""
    ms, base = market["ms"], position["base"]
    log(state, ms, "entry_aborted", base=base, reason=reason)
    position["pending"] = None
    if position["longHeld"]:
        position["phase"] = "aborting"
        intent(state, position, market, ms, "close_long", position["long_symbol"], "Sell", protocol)
    else:
        position["phase"] = "cancelled"


def try_pending(state, position, market, protocol):
    """Fill a pending intent only from a later quote with a newer source time."""
    ms, base, qty = market["ms"], position["base"], D(position["quantity"])
    pending = position["pending"]
    purpose = pending["purpose"]
    if pending["deadlineMs"] is not None and ms >= pending["deadlineMs"]:
        log(state, ms, "intent_expired", base=base, purpose=purpose)
        position["pending"] = None
        if purpose == "open_long":
            position["phase"] = "cancelled"
        else:
            abort_entry(state, position, market, protocol, "short_leg_timeout")
        return
    q = market["quotes"].get(pending["symbol"])
    if q is None:
        log(state, ms, "pending_quote_unavailable", base=base, purpose=purpose)
        return
    if q["sourceMs"] <= pending["decisionSourceMs"]:
        # Same or older source time: not an independent later quote.
        return
    if purpose in OPEN_PURPOSES:
        days = (position["expiryMs"] - ms) / DAY
        if not protocol["min_entry_days"] <= days <= protocol["max_entry_days"]:
            if purpose == "open_long":
                position["pending"] = None
                position["phase"] = "cancelled"
                log(state, ms, "entry_window_ended", base=base, daysRemaining=days)
            else:
                abort_entry(state, position, market, protocol, "entry_window_ended")
            return
    px = price(
        market,
        pending["symbol"],
        pending["side"],
        protocol,
        qty,
        spread_gate=purpose in OPEN_PURPOSES,
    )
    if px is None:
        log(state, ms, "pending_quote_unavailable", base=base, purpose=purpose)
        return
    if pending["limit"] is not None:
        limit = D(pending["limit"])
        if pending["side"] == "Buy" and px > limit or pending["side"] == "Sell" and px < limit:
            log(state, ms, "limit_not_touched", base=base, purpose=purpose, limit=str(limit))
            return
    if purpose == "open_short":
        debit = -D(position["cash"]) - px * qty + fee(market, pending["symbol"], px, qty, protocol)
        if not ZERO < debit <= D(protocol["max_debit_per_trade_usdt"]) or debit >= D(
            position["widthValue"]
        ):
            abort_entry(state, position, market, protocol, "short_leg_debit_out_of_budget")
            return
    if purpose == "open_long" and px * qty > D(protocol["max_long_premium_per_trade_usdt"]):
        position["pending"] = None
        position["phase"] = "cancelled"
        log(state, ms, "long_budget_rejected", base=base)
        return
    fill(position, market, pending, px, protocol, state)
    if purpose == "open_long":
        position["phase"] = "opening_short"
        intent(
            state,
            position,
            market,
            ms,
            "open_short",
            position["short_symbol"],
            "Sell",
            protocol,
            D(position["shortLimit"]),
        )
    elif purpose == "open_short":
        position["phase"] = "open"
        position["openedMs"] = ms
        position["entryDebit"] = str(-D(position["cash"]))
        log(state, ms, "spread_opened", base=base, entryDebit=position["entryDebit"])
    elif purpose == "close_short":
        position["phase"] = "closing_long"
        intent(state, position, market, ms, "close_long", position["long_symbol"], "Sell", protocol)
    else:
        position["phase"] = "aborted" if position["phase"] == "aborting" else "closed"
        position["closedMs"] = ms
        log(
            state,
            ms,
            "position_ended",
            base=base,
            phase=position["phase"],
            netPnl=position["cash"],
            fees=position["fees"],
        )


def check_exit(state, position, market, protocol):
    """Trigger an exit intent; the trigger is not a fill."""
    ms, base = market["ms"], position["base"]
    expiry = position["expiryMs"]
    valuation = value(position, market, protocol)
    reason = None
    if ms - position["openedMs"] >= protocol["max_hold_hours"] * HOUR:
        reason = "max_hold"
    elif expiry - ms <= protocol["close_before_expiry_hours"] * HOUR:
        reason = "before_expiry"
    elif valuation is not None:
        pnl, debit = D(valuation["netPnl"]), D(position["entryDebit"])
        if pnl >= debit * D(protocol["take_profit_debit_fraction"]):
            reason = "take_profit"
        elif pnl <= -debit * D(protocol["stop_loss_debit_fraction"]):
            reason = "stop_loss"
    if reason is None:
        return
    position["phase"] = "closing_short"
    position["exitReason"] = reason
    log(state, ms, "exit_trigger", base=base, reason=reason, valuation=valuation)
    intent(state, position, market, ms, "close_short", position["short_symbol"], "Buy", protocol)


def step_position(state, position, market, protocol):
    ms, base = market["ms"], position["base"]
    if position["phase"] in DONE:
        return
    expiry = position.get("expiryMs")
    if expiry is None:
        instrument = market["instruments"].get(position["long_symbol"])
        if instrument is None:
            log(state, ms, "instrument_unavailable", base=base)
            return
        expiry = int(instrument["deliveryTime"])
    if ms >= expiry:
        held = position["longHeld"] or position["shortHeld"]
        position["phase"] = "expired_unresolved" if held else "cancelled"
        position["pending"] = None
        log(state, ms, position["phase"], base=base)
        return
    if not contract_ok(market, position):
        log(state, ms, "instrument_ineligible", base=base)
        return
    phase = position["phase"]
    if phase == "waiting":
        decide_entry(state, position, market, protocol)
        return
    if position["pending"] is not None:
        try_pending(state, position, market, protocol)
        return
    if phase == "open":
        check_exit(state, position, market, protocol)


def apply(state, market, protocol, snapshot_hash, path):
    """Apply one snapshot to the ledger. Replaying the same snapshot changes nothing."""
    if snapshot_hash in state["processed"]:
        return state
    ms = market["ms"]
    if state["lastMs"] is not None and ms <= state["lastMs"]:
        raise ValueError("Non-increasing source time")
    state = copy.deepcopy(state)
    if state["startedMs"] is None:
        state["startedMs"] = ms
        log(state, ms, "study_started", simulation=True, marginValidated=False)
    elif ms - state["lastMs"] > protocol["max_observation_gap_seconds"] * 1000:
        log(
            state,
            ms,
            "observation_gap",
            previousMs=state["lastMs"],
            gapSeconds=(ms - state["lastMs"]) / 1000,
        )
    if ms - state["startedMs"] >= protocol["study_days"] * DAY:
        for pos in state["positions"].values():
            if pos["phase"] == "waiting":
                pos["phase"] = "cancelled"
                log(state, ms, "study_entry_closed", base=pos["base"])
    for position in state["positions"].values():
        step_position(state, position, market, protocol)
    valuations = {b: value(p, market, protocol) for b, p in state["positions"].items()}
    state["samples"].append(
        {
            "utc": iso(ms),
            "ms": ms,
            "snapshotHash": snapshot_hash,
            "signals": market["signals"],
            "valuations": valuations,
            "phases": {b: p["phase"] for b, p in state["positions"].items()},
        }
    )
    state["processed"][snapshot_hash] = {"path": str(path), "ms": ms}
    state["lastMs"] = ms
    return state


def report(state, path):
    lines = [
        "# BTC/ETH 前向纸面验证状态",
        "",
        "**模拟研究；未验证交易所保证金、清算或真实成交。**",
        "",
        f"协议：{state['protocolVersion']}；SHA-256：`{state['protocolHash']}`。",
        f"首次观察：{iso(state['startedMs']) if state['startedMs'] else '尚未开始'}。",
        f"最近观察：{iso(state['lastMs']) if state['lastMs'] else '无'}；"
        f"快照数：{len(state['processed'])}。",
        "",
        "| 标的 | 阶段 | 长腿 | 短腿 | 净借记 | 已收付净现金 | 累计费用 | 含平仓成本净损益估值 |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    sample = state["samples"][-1] if state["samples"] else {"valuations": {}}
    labels = {
        "waiting": "等待信号",
        "opening_long": "等待买长腿",
        "opening_short": "长腿已买/等待短腿",
        "open": "价差持仓",
        "closing_short": "等待平空腿",
        "closing_long": "等待平长腿",
        "closed": "已平仓",
        "cancelled": "未成交取消",
        "aborting": "退出单腿",
        "aborted": "执行中止",
        "expired_unresolved": "到期未核对",
    }
    for base, p in state["positions"].items():
        v = sample["valuations"].get(base)
        pnl = f"{D(v['netPnl']):.4f}" if v else "未知"
        held_long = "持有" if p["longHeld"] else "无"
        held_short = "持有" if p["shortHeld"] else "无"
        debit = f"{D(p['entryDebit']):.4f}" if "entryDebit" in p else "—"
        lines.append(
            f"| {base} | {labels[p['phase']]} | {held_long} | {held_short} | "
            f"{debit} | {D(p['cash']):.4f} | {D(p['fees']):.4f} | {pnl} |"
        )
    lines.extend(["", "## 已观察损益与逐腿成交", ""])
    for base in state["positions"]:
        observed = [
            D(s["valuations"][base]["netPnl"])
            for s in state["samples"]
            if s["valuations"][base] is not None
        ]
        losses = min([ZERO, *observed])
        unknown = sum(s["valuations"][base] is None for s in state["samples"])
        lines.append(
            f"- {base}：已观察最大净浮亏 {abs(losses):.4f} USDT；"
            f"未知估值 {unknown} 次。采样之间的最大亏损未知。"
        )
    lines.extend(
        [
            "",
            "| 时间 UTC | 标的 | 动作 | 合约 | 数量 | 成交价 | 费用 | 净现金流 | 间隔(秒) |",
            "|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    decisions = {}
    for e in state["events"]:
        if e["kind"] == "intent":
            decisions[(e["base"], e["purpose"])] = e
        elif e["kind"] == "simulated_fill":
            decision = decisions.get((e["base"], e["purpose"]))
            elapsed = (
                f"{(e['quoteSourceMs'] - decision['decisionSourceMs']) / 1000:.0f}"
                if decision
                else "—"
            )
            lines.append(
                f"| {e['utc']} | {e['base']} | {e['purpose']} | {e['symbol']} | "
                f"{e['quantity']} | {e['price']} | {D(e['fee']):.6f} | "
                f"{D(e['cashFlow']):.6f} | {elapsed} |"
            )
    gaps = sum(e["kind"] == "observation_gap" for e in state["events"])
    lines.extend(
        [
            "",
            f"超过30分钟的采样缺口：{gaps}。缺口期间不推测触发或成交。",
            "金额均为USDT；现金流不等于盈亏。持仓估值已扣假设平仓费与滑点。",
            "已平仓净现金为已实现净损益；缺价则估值未知；到期未核对不能计作已平仓。",
            "",
            "## 最近事件",
            "",
        ]
    )
    for e in state["events"][-16:]:
        lines.append(
            f"- {e['utc']} {e.get('base', '')} {e['kind']} {e.get('purpose', e.get('reason', ''))}"
        )
    lines.extend(
        [
            "",
            "协议见 PROTOCOL.md。每个标的最多一组，不能外推年化或持续盈利。",
            "运行进程依赖本机与网络；最后观察时间过旧表示采样可能中断。",
            "成交均使用独立采集、源时间更晚的新盘口；决策到成交的实际间隔按上表如实记录。",
            "这些数据可用于模拟定价回测，但不能替代历史期权真实报价。",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def process(path, protocol, protocol_hash, output):
    state_file = output / "state.json"
    state = (
        json.loads(state_file.read_text())
        if state_file.exists()
        else new_state(protocol, protocol_hash)
    )
    if state_file.exists() and state.get("checkpointHash") != state_hash(state):
        raise IntegrityError("Checkpoint checksum mismatch")
    if state["protocolHash"] != protocol_hash:
        raise IntegrityError("Protocol changed: create a new study")
    for h, record in state["processed"].items():
        if digest(Path(record["path"])) != h:
            raise IntegrityError("Historical snapshot changed")
    snapshot_hash = digest(path)
    snapshot = json.loads(path.read_text())
    if (output / "manifest.json").exists():
        manifest = prepare_run(output, protocol_hash)
        if stamp(snapshot["startedUtc"]) < stamp(manifest["frozenUtc"]):
            raise IntegrityError("Snapshot predates the frozen protocol")
    market = view(snapshot, protocol)
    state = apply(state, market, protocol, snapshot_hash, path.resolve())
    state["checkpointHash"] = state_hash(state)
    atomic_json(state_file, state)
    report(state, output / "STATUS.md")
    print(
        json.dumps(
            {
                "utc": iso(market["ms"]),
                "snapshots": len(state["processed"]),
                "positions": {
                    b: {"phase": p["phase"], "cash": p["cash"], "fees": p["fees"]}
                    for b, p in state["positions"].items()
                },
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--run-dir", type=Path, default=ROOT / "run-v1")
    args = parser.parse_args()
    protocol, protocol_hash = load_protocol()
    output = args.run_dir
    output.mkdir(parents=True, exist_ok=True)
    with (output / "runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = prepare_run(output, protocol_hash)
        end_ms = stamp(manifest["frozenUtc"]) + protocol["study_days"] * DAY
        while int(time.time() * 1000) < end_ms:
            try:
                prepare_run(output, protocol_hash)
                snapshot = args.snapshot or collect(protocol, ROOT / "snapshots")
                state = process(snapshot, protocol, protocol_hash, output)
                atomic_json(
                    output / "heartbeat.json", {"status": "ok", "utc": iso(state["lastMs"])}
                )
                if all(p["phase"] in DONE for p in state["positions"].values()):
                    print("STUDY_ENDED: all positions terminal", flush=True)
                    break
                if state["lastMs"] - state["startedMs"] >= protocol["study_days"] * DAY:
                    print(
                        "STUDY_ENDED: time bound reached; inspect any unresolved positions",
                        flush=True,
                    )
                    break
            except Exception as error:
                atomic_json(
                    output / "heartbeat.json",
                    {
                        "status": "error",
                        "utc": dt.datetime.now(dt.UTC).isoformat(),
                        "error": str(error),
                    },
                )
                print(f"COLLECTION_OR_LEDGER_ERROR: {type(error).__name__}: {error}", flush=True)
                if isinstance(error, IntegrityError) or not args.watch:
                    raise
            if not args.watch or args.snapshot:
                break
            remaining = max(0, (end_ms - int(time.time() * 1000)) / 1000)
            time.sleep(min(protocol["sampling_seconds"], remaining))
        else:
            atomic_json(
                output / "heartbeat.json",
                {
                    "status": "ended",
                    "utc": dt.datetime.now(dt.UTC).isoformat(),
                    "reason": "five-day wall-clock bound reached; inspect unresolved positions",
                },
            )
            print("STUDY_ENDED: five-day wall-clock bound reached", flush=True)


if __name__ == "__main__":
    main()
