"""Deterministic protocol tests; all markets and raw responses are synthetic."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("forward.py")
START = int(dt.datetime(2026, 11, 15, 12, 15, tzinfo=dt.UTC).timestamp() * 1000)
EXPIRY = int(dt.datetime(2026, 11, 27, 8, tzinfo=dt.UTC).timestamp() * 1000)
HOUR = 3_600_000
DAY = 24 * HOUR
D = Decimal


@pytest.fixture(scope="module")
def engine():
    spec = importlib.util.spec_from_file_location("options_forward_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_collection(engine, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Deterministic tests must not collect data or start subprocesses")

    monkeypatch.setattr(engine, "collect", forbidden)
    monkeypatch.setattr(engine.subprocess, "run", forbidden)


@pytest.fixture
def protocol():
    frozen = json.loads(SCRIPT.with_name("protocol.json").read_text())
    frozen["positions"] = [copy.deepcopy(frozen["positions"][0])]
    return frozen


@pytest.fixture
def market_factory(protocol):
    spec = protocol["positions"][0]
    quantity = spec["quantity"]

    def make(ms=START, *, source_ms=None, long=None, short=None, eligible=True):
        quotes = {}
        instruments = {}
        for symbol, bid, ask, changes in (
            (spec["long_symbol"], "1000", "1002", long),
            (spec["short_symbol"], "800", "802", short),
        ):
            quotes[symbol] = {
                "symbol": symbol,
                "bid1Price": bid,
                "ask1Price": ask,
                "bid1Size": quantity,
                "ask1Size": quantity,
                "indexPrice": "60000",
                "markPrice": "999999",
                "sourceMs": ms if source_ms is None else source_ms,
                **(changes or {}),
            }
            instruments[symbol] = {
                "symbol": symbol,
                "baseCoin": spec["base"],
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "optionsType": "Put",
                "status": "Trading",
                "deliveryTime": str(EXPIRY),
                "lotSizeFilter": {"minOrderQty": quantity, "qtyStep": quantity},
                "priceFilter": {"tickSize": "1", "minPrice": "1", "maxPrice": "1000000"},
            }
        return {
            "ms": ms,
            "quotes": quotes,
            "instruments": instruments,
            "signals": {
                spec["base"]: {
                    "eligible": eligible,
                    "hourClose": "58000",
                    "sma20": "59000",
                    "sma50": "59000",
                    "return24h": str(D("58000") / D("60000") - 1),
                }
            },
        }

    return make


@pytest.fixture
def ledger(engine, protocol, tmp_path):
    protocol_hash = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()

    def apply(state, market):
        data = json.dumps(market, sort_keys=True)
        snapshot_hash = hashlib.sha256(data.encode()).hexdigest()
        path = tmp_path / f"{snapshot_hash}.json"
        path.write_text(data)
        return engine.apply(state, market, protocol, snapshot_hash, path)

    return engine.new_state(protocol, protocol_hash), apply


@pytest.fixture
def opened(ledger, market_factory):
    state, apply = ledger
    for offset in (0, 1000, 2000):
        state = apply(state, market_factory(START + offset))
    assert position(state)["phase"] == "open"
    return state, apply


def position(state):
    return next(iter(state["positions"].values()))


def fills(state):
    return [event for event in state["events"] if event["kind"] == "simulated_fill"]


def events(state, kind):
    return [event for event in state["events"] if event["kind"] == kind]


def raw_snapshot(market, protocol, *, unfinished=False):
    """Construct the documented public-response shape without contacting Bybit."""
    ms = market["ms"]
    received = dt.datetime.fromtimestamp((ms + 500) / 1000, dt.UTC).isoformat()
    completed = dt.datetime.fromtimestamp((ms + 1000) / 1000, dt.UTC).isoformat()
    records = []

    def record(path, params, rows):
        records.append(
            {
                "path": path,
                "params": params,
                "receivedUtc": received,
                "payload": {"retCode": 0, "time": ms, "result": {"list": rows}},
            }
        )

    record("/v5/market/time", {}, [])
    for spec in protocol["positions"]:
        base = spec["base"]
        record(
            "/v5/market/tickers",
            {"category": "option", "baseCoin": base},
            [
                copy.deepcopy(market["quotes"][symbol])
                for symbol in (spec["long_symbol"], spec["short_symbol"])
            ],
        )
        for symbol in (spec["long_symbol"], spec["short_symbol"]):
            record(
                "/v5/market/instruments-info",
                {"category": "option", "symbol": symbol},
                [copy.deepcopy(market["instruments"][symbol])],
            )
        for interval, width, count in (("60", HOUR, 25), ("D", DAY, 50)):
            boundary = ms // width * width
            rows = []
            for number in range(count):
                close = "59000" if interval == "D" else "60000"
                if interval == "60" and number == count - 1:
                    close = "58000"
                rows.append(
                    [
                        str(boundary - (count - number) * width),
                        close,
                        close,
                        close,
                        close,
                        "1",
                        "1",
                    ]
                )
            if unfinished:
                # Both would overturn the signal if treated as completed history.
                close = "999999" if interval == "60" else "1"
                rows.append([str(boundary), close, close, close, close, "1", "1"])
            record(
                "/v5/market/kline",
                {"category": "spot", "symbol": base + "USDT", "interval": interval},
                list(reversed(rows)),
            )
    return {"startedUtc": received, "completedUtc": completed, "records": records}


def test_intent_has_no_same_snapshot_fill_and_replay_is_idempotent(ledger, market_factory):
    initial, apply = ledger
    market = market_factory()
    before = copy.deepcopy(initial)
    state = apply(initial, market)
    assert initial == before
    assert position(state)["phase"] == "opening_long"
    assert position(state)["cash"] == "0"
    assert not position(state)["longHeld"]
    assert not fills(state)
    assert len(state["samples"]) == 1
    assert apply(state, market) == state


def test_long_then_short_require_distinct_newer_quote_sources(ledger, market_factory):
    state, apply = ledger
    state = apply(state, market_factory())
    state = apply(state, market_factory(START + 1000, source_ms=START))
    assert not fills(state)
    state = apply(state, market_factory(START + 2000))
    assert [fill["purpose"] for fill in fills(state)] == ["open_long"]
    assert position(state)["longHeld"] and not position(state)["shortHeld"]
    assert position(state)["pending"]["purpose"] == "open_short"
    state = apply(state, market_factory(START + 3000, source_ms=START + 2000))
    assert len(fills(state)) == 1
    state = apply(state, market_factory(START + 4000))
    assert [fill["purpose"] for fill in fills(state)] == ["open_long", "open_short"]
    assert [fill["quoteSourceMs"] for fill in fills(state)] == [START + 2000, START + 4000]
    assert position(state)["phase"] == "open"
    assert D(position(state)["entryDebit"]) == D("2.40")


@pytest.mark.parametrize(
    "index,px,expected",
    [
        ("60000", "1003", "0.18"),  # Index-based taker fee is lower.
        ("60000", "10", "0.007"),  # Seven-percent premium cap is lower.
    ],
)
def test_fee_uses_decimal_and_both_fee_branches(
    engine,
    protocol,
    market_factory,
    index,
    px,
    expected,
):
    market = market_factory(long={"indexPrice": index})
    symbol = protocol["positions"][0]["long_symbol"]
    result = engine.fee(market, symbol, D(px), D("0.01"), protocol)
    assert isinstance(result, D)
    assert result == D(expected)


def test_premium_capped_fees_flow_through_both_opening_legs(ledger, market_factory):
    state, apply = ledger
    for offset in (0, 1000, 2000):
        state = apply(
            state,
            market_factory(
                START + offset,
                long={"bid1Price": "100", "ask1Price": "102"},
                short={"bid1Price": "80", "ask1Price": "82"},
            ),
        )
    long, short = fills(state)
    assert (D(long["price"]), D(short["price"])) == (D("103"), D("79"))
    assert (D(long["fee"]), D(short["fee"])) == (D("0.0721"), D("0.0553"))
    assert D(position(state)["cash"]) == D("-0.3674")
    assert D(position(state)["fees"]) == D("0.1274")
    assert D(position(state)["entryDebit"]) == D("0.3674")


def test_open_cash_flows_include_slippage_and_each_leg_fee(opened):
    state, _ = opened
    long, short = fills(state)
    assert (D(long["price"]), D(short["price"])) == (D("1003"), D("799"))
    assert (D(long["cashFlow"]), D(short["cashFlow"])) == (D("-10.21"), D("7.81"))
    assert D(position(state)["cash"]) == D("-2.40")
    assert D(position(state)["fees"]) == D("0.36")
    valuation = state["samples"][-1]["valuations"][position(state)["base"]]
    assert D(valuation["liquidationValue"]) == D("1.60")
    assert D(valuation["netPnl"]) == D("-0.80")


def test_take_profit_trigger_and_short_then_long_exit_on_new_sources(opened, market_factory):
    state, apply = opened
    profitable = {"bid1Price": "1300", "ask1Price": "1302"}
    trigger_ms = START + 3000
    state = apply(state, market_factory(trigger_ms, long=profitable))
    assert position(state)["exitReason"] == "take_profit"
    assert position(state)["phase"] == "closing_short"
    assert len(fills(state)) == 2
    state = apply(state, market_factory(START + 4000, source_ms=trigger_ms, long=profitable))
    assert len(fills(state)) == 2
    state = apply(state, market_factory(START + 5000, long=profitable))
    assert [fill["purpose"] for fill in fills(state)][-1] == "close_short"
    assert not position(state)["shortHeld"] and position(state)["longHeld"]
    assert position(state)["pending"]["purpose"] == "close_long"
    state = apply(state, market_factory(START + 6000, source_ms=START + 5000, long=profitable))
    assert len(fills(state)) == 3
    state = apply(state, market_factory(START + 7000, long=profitable))
    assert [fill["purpose"] for fill in fills(state)] == [
        "open_long",
        "open_short",
        "close_short",
        "close_long",
    ]
    assert position(state)["phase"] == "closed"
    assert not position(state)["shortHeld"] and not position(state)["longHeld"]
    assert D(position(state)["cash"]) == D("2.20")
    assert D(position(state)["fees"]) == D("0.72")
    # One trade per asset: a subsequent eligible signal cannot re-enter.
    later = apply(state, market_factory(START + 8000))
    assert len(fills(later)) == 4
    assert position(later)["phase"] == "closed"


def test_exit_intent_survives_the_entry_ttl_and_still_fills_later(opened, market_factory):
    state, apply = opened
    profitable = {"bid1Price": "1300", "ask1Price": "1302"}
    trigger_ms = START + 3000
    state = apply(state, market_factory(trigger_ms, long=profitable))
    assert position(state)["pending"]["purpose"] == "close_short"
    assert position(state)["pending"]["deadlineMs"] is None
    later = trigger_ms + 45 * 60_000
    state = apply(state, market_factory(later, long=profitable))
    assert [fill["purpose"] for fill in fills(state)][-1] == "close_short"
    assert not position(state)["shortHeld"] and position(state)["longHeld"]


def test_stop_loss_triggers_without_filling_on_trigger_snapshot(opened, market_factory):
    state, apply = opened
    state = apply(
        state,
        market_factory(
            START + 3000,
            long={"bid1Price": "900", "ask1Price": "902"},
        ),
    )
    assert position(state)["exitReason"] == "stop_loss"
    assert position(state)["pending"]["purpose"] == "close_short"
    assert len(fills(state)) == 2
    assert D(events(state, "exit_trigger")[-1]["valuation"]["netPnl"]) == D("-1.80")


@pytest.mark.parametrize("reason", ["max_hold", "before_expiry"])
def test_time_exit_triggers_even_without_an_executable_valuation(opened, market_factory, reason):
    state, apply = opened
    if reason == "max_hold":
        trigger_ms = position(state)["openedMs"] + 72 * HOUR
    else:
        trigger_ms = EXPIRY - 24 * HOUR
        # Isolate the expiry deadline from the independently tested holding deadline.
        position(state)["openedMs"] = trigger_ms - HOUR
    market = market_factory(trigger_ms)
    market["quotes"].clear()
    state = apply(state, market)
    assert position(state)["exitReason"] == reason
    assert position(state)["pending"]["purpose"] == "close_short"
    assert len(fills(state)) == 2
    assert events(state, "exit_trigger")[-1]["valuation"] is None


def test_long_open_intent_times_out_without_a_fill(ledger, market_factory, protocol):
    state, apply = ledger
    state = apply(state, market_factory())
    deadline = START + protocol["pending_ttl_seconds"] * 1000
    state = apply(state, market_factory(deadline))
    assert position(state)["phase"] == "cancelled"
    assert position(state)["pending"] is None
    assert D(position(state)["cash"]) == 0
    assert not fills(state)
    assert events(state, "intent_expired")[-1]["purpose"] == "open_long"


def test_short_open_timeout_unwinds_long_only_on_subsequent_quote(ledger, market_factory, protocol):
    state, apply = ledger
    state = apply(state, market_factory())
    state = apply(state, market_factory(START + 1000))
    deadline = START + 1000 + protocol["pending_ttl_seconds"] * 1000
    state = apply(state, market_factory(deadline))
    assert position(state)["phase"] == "aborting"
    assert position(state)["pending"]["purpose"] == "close_long"
    assert len(fills(state)) == 1
    state = apply(state, market_factory(deadline + 1000, source_ms=deadline))
    assert len(fills(state)) == 1
    state = apply(state, market_factory(deadline + 2000))
    assert [fill["purpose"] for fill in fills(state)] == ["open_long", "close_long"]
    assert position(state)["phase"] == "aborted"
    assert not position(state)["longHeld"] and not position(state)["shortHeld"]
    assert "entryDebit" not in position(state)
    assert D(position(state)["cash"]) == D("-0.40")
    assert D(position(state)["fees"]) == D("0.36")


@pytest.mark.parametrize(
    "bad_quote",
    [
        None,
        {"bid1Price": "1003", "ask1Price": "1002"},
        {"bid1Size": "0.009"},
        {"ask1Size": "0.009"},
        {"bid1Price": "0"},
        {"ask1Price": "NaN"},
        {"indexPrice": "0"},
        {"bid1Price": "900", "ask1Price": "1002"},
    ],
)
def test_missing_crossed_undersized_or_invalid_open_quote_cannot_fill(
    ledger,
    market_factory,
    bad_quote,
):
    state, apply = ledger
    state = apply(state, market_factory())
    market = market_factory(START + 1000, long=bad_quote)
    symbol = position(state)["long_symbol"]
    if bad_quote is None:
        del market["quotes"][symbol]
    state = apply(state, market)
    assert position(state)["phase"] == "opening_long"
    assert not fills(state)
    assert D(position(state)["cash"]) == 0


@pytest.mark.parametrize(
    "bad_quote",
    [
        {"bid1Price": "1000", "ask1Price": "1002.5"},
        {"bid1Price": "999.5", "ask1Price": "1002"},
    ],
)
def test_off_tick_quote_cannot_fill(ledger, market_factory, bad_quote):
    state, apply = ledger
    state = apply(state, market_factory())
    state = apply(state, market_factory(START + 1000, long=bad_quote))
    assert not fills(state)
    assert position(state)["phase"] == "opening_long"


@pytest.mark.parametrize("bound", ["minPrice", "maxPrice", "slippage_max"])
def test_instrument_price_bounds_prevent_fill(ledger, market_factory, bound):
    state, apply = ledger
    state = apply(state, market_factory())
    market = market_factory(START + 1000)
    price_filter = market["instruments"][position(state)["long_symbol"]]["priceFilter"]
    if bound == "minPrice":
        # Bounds apply to the post-slippage fill price of 1003.
        price_filter["minPrice"] = "1004"
    elif bound == "maxPrice":
        price_filter["maxPrice"] = "1001"
    else:
        # Raw quote is in range; the assumed adverse tick must be in range too.
        price_filter["maxPrice"] = "1002"
    state = apply(state, market)
    assert not fills(state)
    assert position(state)["phase"] == "opening_long"


@pytest.mark.parametrize("leg", ["long", "short"])
def test_one_tick_slippage_beyond_entry_limit_prevents_fill(ledger, market_factory, leg):
    state, apply = ledger
    state = apply(state, market_factory())
    assert D(position(state)["longLimit"]) == D("1023")
    assert D(position(state)["shortLimit"]) == D("784")
    if leg == "long":
        # Ask exactly at the limit still becomes an unacceptable 1024 fill.
        market = market_factory(START + 1000, long={"bid1Price": "1021", "ask1Price": "1023"})
        expected_fills = 0
    else:
        state = apply(state, market_factory(START + 1000))
        # Bid exactly at the limit becomes an unacceptable 783 fill.
        market = market_factory(START + 2000, short={"bid1Price": "784", "ask1Price": "786"})
        expected_fills = 1
    state = apply(state, market)
    assert len(fills(state)) == expected_fills
    assert events(state, "limit_not_touched")[-1]["purpose"] == f"open_{leg}"


def test_unavailable_held_quote_has_unknown_value_not_mark_price(opened, market_factory):
    state, apply = opened
    market = market_factory(START + 3000)
    del market["quotes"][position(state)["short_symbol"]]
    state = apply(state, market)
    assert state["samples"][-1]["valuations"][position(state)["base"]] is None
    assert position(state)["phase"] == "open"
    assert len(fills(state)) == 2


@pytest.mark.parametrize("gap_ms,expected_gaps", [(30 * 60_000, 0), (30 * 60_000 + 1, 1)])
def test_observation_gap_is_explicit_without_invented_intermediate_fills(
    ledger,
    market_factory,
    gap_ms,
    expected_gaps,
):
    state, apply = ledger
    state = apply(state, market_factory(eligible=False))
    state = apply(state, market_factory(START + gap_ms, eligible=False))
    gaps = events(state, "observation_gap")
    assert len(gaps) == expected_gaps
    assert len(state["samples"]) == 2
    assert not fills(state)
    assert not events(state, "exit_trigger")
    if gaps:
        assert gaps[0]["previousMs"] == START
        assert D(str(gaps[0]["gapSeconds"])) == D(gap_ms) / 1000


@pytest.mark.parametrize("offset", [0, -1])
def test_different_snapshot_with_nonincreasing_time_is_rejected(ledger, market_factory, offset):
    state, apply = ledger
    state = apply(state, market_factory())
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        apply(state, market_factory(START + offset, eligible=False))
    assert state == before


@pytest.mark.parametrize("status", ["Trading", "Settled"])
def test_expiry_preserves_unresolved_cash_and_holdings_without_fabricating_settlement(
    opened,
    market_factory,
    status,
):
    state, apply = opened
    before = copy.deepcopy(position(state))
    market = market_factory(EXPIRY)
    for instrument in market["instruments"].values():
        instrument["status"] = status
    state = apply(state, market)
    pos = position(state)
    assert pos["phase"] == "expired_unresolved"
    assert pos["pending"] is None
    assert pos["cash"] == before["cash"]
    assert pos["fees"] == before["fees"]
    assert pos["longHeld"] and pos["shortHeld"]
    assert len(fills(state)) == 2
    assert "closedMs" not in pos
    assert events(state, "expired_unresolved")
    assert state["samples"][-1]["valuations"][pos["base"]] is None


def test_view_excludes_unfinished_hourly_and_daily_candles(engine, protocol, market_factory):
    raw = raw_snapshot(market_factory(), protocol, unfinished=True)
    market = engine.view(raw, protocol)
    signal = market["signals"][protocol["positions"][0]["base"]]
    assert signal["eligible"] is True
    assert D(signal["hourClose"]) == D("58000")
    assert D(signal["sma20"]) == D("59000")
    assert D(signal["sma50"]) == D("59000")
    assert D(signal["return24h"]) == D("58000") / D("60000") - 1
    assert {q["sourceMs"] for q in market["quotes"].values()} == {START}


@pytest.mark.parametrize("mode", ["stale_at_receipt", "future", "stale_at_completion"])
def test_view_rejects_stale_future_and_skewed_raw_responses(
    engine,
    protocol,
    market_factory,
    mode,
):
    raw = raw_snapshot(market_factory(), protocol)
    record = raw["records"][1]
    if mode == "future":
        record["payload"]["time"] = START + 2000
    else:
        record["payload"]["time"] = START - 31_000
        if mode == "stale_at_completion":
            record["receivedUtc"] = dt.datetime.fromtimestamp(
                (START - 30_500) / 1000,
                dt.UTC,
            ).isoformat()
    with pytest.raises(ValueError):
        engine.view(raw, protocol)


@pytest.mark.parametrize("interval", ["60", "D"])
def test_view_rejects_gaps_in_completed_signal_history(engine, protocol, market_factory, interval):
    raw = raw_snapshot(market_factory(), protocol)
    record = next(
        r
        for r in raw["records"]
        if r["path"] == "/v5/market/kline" and r["params"]["interval"] == interval
    )
    del record["payload"]["result"]["list"][5]
    with pytest.raises(ValueError):
        engine.view(raw, protocol)


def test_process_detects_tampered_checkpoint(engine, protocol, market_factory, tmp_path):
    snapshot = tmp_path / "first.json"
    snapshot.write_text(json.dumps(raw_snapshot(market_factory(), protocol)))
    protocol_hash = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
    output = tmp_path / "ledger"
    engine.process(snapshot, protocol, protocol_hash, output)
    saved = json.loads((output / "state.json").read_text())
    assert saved["checkpointHash"] == engine.state_hash(saved)
    saved["positions"][protocol["positions"][0]["base"]]["cash"] = "999"
    (output / "state.json").write_text(json.dumps(saved))
    next_snapshot = tmp_path / "next.json"
    next_snapshot.write_text(json.dumps(raw_snapshot(market_factory(START + 1000), protocol)))
    with pytest.raises(ValueError, match="[Cc]heckpoint"):
        engine.process(next_snapshot, protocol, protocol_hash, output)


@pytest.mark.parametrize("interval", ["60", "D"])
def test_view_rejects_invalid_completed_ohlc(engine, protocol, market_factory, interval):
    raw = raw_snapshot(market_factory(), protocol)
    record = next(
        r
        for r in raw["records"]
        if r["path"] == "/v5/market/kline" and r["params"]["interval"] == interval
    )
    row = record["payload"]["result"]["list"][0]
    row[1:5] = ["100", "90", "95", "98"]  # High below low.
    with pytest.raises(ValueError):
        engine.view(raw, protocol)


@pytest.mark.parametrize("interval", ["60", "D"])
def test_view_rejects_misaligned_candle_timestamps(engine, protocol, market_factory, interval):
    raw = raw_snapshot(market_factory(), protocol)
    record = next(
        r
        for r in raw["records"]
        if r["path"] == "/v5/market/kline" and r["params"]["interval"] == interval
    )
    row = record["payload"]["result"]["list"][-1]
    row[0] = str(int(row[0]) + 1)
    with pytest.raises(ValueError):
        engine.view(raw, protocol)


@pytest.mark.parametrize("tamper", ["protocol", "history"])
def test_process_rejects_protocol_or_historical_snapshot_hash_change(
    engine,
    protocol,
    market_factory,
    tmp_path,
    tamper,
):
    snapshot = tmp_path / "first.json"
    snapshot.write_text(json.dumps(raw_snapshot(market_factory(), protocol)))
    protocol_hash = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
    output = tmp_path / "ledger"
    state = engine.process(snapshot, protocol, protocol_hash, output)
    assert engine.process(snapshot, protocol, protocol_hash, output) == state
    saved = (output / "state.json").read_bytes()
    next_snapshot = tmp_path / "next.json"
    next_snapshot.write_text(json.dumps(raw_snapshot(market_factory(START + 1000), protocol)))
    if tamper == "protocol":
        protocol = copy.deepcopy(protocol)
        protocol["max_hold_hours"] += 1
        protocol_hash = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
    else:
        snapshot.write_text(snapshot.read_text() + "\n")
    with pytest.raises(ValueError, match="[Pp]rotocol|[Hh]istor"):
        engine.process(next_snapshot, protocol, protocol_hash, output)
    assert (output / "state.json").read_bytes() == saved
