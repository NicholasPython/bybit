"""Synthetic state machine cases; no network calls or exchange orders."""

import copy
import importlib.util
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("bull_forward_test", ROOT / "forward.py")
f = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(f)
P = json.loads((ROOT / "protocol.json").read_text())
NOW = 1_794_744_000_000


def option(base, strike, delta, bid, ask, *, expiry=None):
    return {
        "symbol": f"{base}-TEST-{strike}-C-USDT",
        "base": base,
        "status": "Trading",
        "expiryMs": expiry or NOW + 21 * f.DAY,
        "strike": D(str(strike)),
        "delta": D(str(delta)),
        "index": D(1000),
        "bid": D(str(bid)),
        "ask": D(str(ask)),
        "bidSize": D(1),
        "askSize": D(1),
        "tick": D(".1"),
        "minQty": D(".01"),
        "step": D(".01"),
        "maxQty": D(10),
        "minPrice": D(".1"),
        "maxPrice": D(10000),
        "sourceMs": NOW,
    }


def market():
    options = {}
    for base in P["bases"]:
        for row in (option(base, 1000, ".55", "29.9", 30), option(base, 1050, ".35", 10, "10.1")):
            options[row["symbol"]] = row
    signals = {
        b: {
            "eligible": True,
            "dailyClose": "1100",
            "sma20": "1000",
            "previousSma20": "990",
            "sma50": "980",
            "last3HourlyCloses": ["1100"] * 3,
            "return24h": ".1",
        }
        for b in P["bases"]
    }
    return {
        "ms": NOW,
        "options": options,
        "signals": signals,
        "conflicts": dict.fromkeys(P["bases"], False),
    }


def state():
    return f.new_state(P, {"files": {"protocol.json": "test"}, "frozenUtc": f.iso(NOW - 1000)})


def tick(s, m, seconds=900, *, new_source=True):
    m["ms"] += seconds * 1000
    if new_source:
        for row in m["options"].values():
            row["sourceMs"] = m["ms"]
    return f.apply(s, m, P, str(m["ms"]), {"path": "synthetic"})


def opened():
    s, m = state(), market()
    s = f.apply(s, m, P, "initial", {})
    s = tick(s, m)
    s = tick(s, m)
    assert s["positions"]["BTC"]["phase"] == "open"
    return s, m


def test_no_entry_without_strength_or_with_opposing_exposure():
    for gate in ("signal", "opposing"):
        s, m = state(), market()
        if gate == "signal":
            for signal in m["signals"].values():
                signal["eligible"] = False
        else:
            m["conflicts"] = dict.fromkeys(P["bases"], True)
        s = f.apply(s, m, P, gate, {})
        assert all(p["phase"] == "waiting" for p in s["positions"].values())
        assert not s["events"]


def test_pooled_budget_and_separate_quotes_for_each_leg():
    s, m = state(), market()
    s = f.apply(s, m, P, "initial", {})
    assert not any(e["kind"] == "simulated_fill" for e in s["events"])
    assert sum(D(p["reservedRisk"]) for p in s["positions"].values()) <= 25
    assert s["positions"]["BTC"]["quantity"] == "1"
    assert D(s["positions"]["ETH"]["quantity"]) < 1
    old = copy.deepcopy(s)
    s = tick(s, m, new_source=False)
    assert all(not p["longHeld"] for p in s["positions"].values())
    s = tick(old, m, seconds=1)
    assert s["positions"]["BTC"]["longHeld"]
    assert not s["positions"]["BTC"]["shortHeld"]
    assert s["positions"]["BTC"]["pending"]["purpose"] == "open_short"
    s = tick(s, m)
    assert s["positions"]["BTC"]["shortHeld"]
    for base, p in s["positions"].items():
        fills = [e for e in s["events"] if e["kind"] == "simulated_fill" and e["base"] == base]
        assert len(fills) == 2
        assert D(p["cash"]) == sum(D(e["cashFlow"]) for e in fills)
        assert D(p["fees"]) == sum(D(e["fee"]) for e in fills)
        assert all(e["quoteSourceMs"] > e["decisionSourceMs"] for e in fills)


def test_idempotency_and_nonincreasing_time():
    s, m = state(), market()
    s = f.apply(s, m, P, "one", {})
    assert f.apply(s, m, P, "one", {}) is s
    with pytest.raises(ValueError, match="Non-increasing"):
        f.apply(s, m, P, "two", {})


def test_deterministic_candidate_expiry_then_delta():
    m = market()
    near = option("BTC", 1000, ".60", "29.9", 30, expiry=NOW + 20 * f.DAY)
    short = option("BTC", 1050, ".35", 10, "10.1", expiry=near["expiryMs"])
    near["symbol"] += "earlier"
    short["symbol"] += "earlier"
    m["options"].update({o["symbol"]: o for o in (near, short)})
    picked = f.choose("BTC", m, D(25), P)
    assert picked[1]["expiryMs"] == NOW + 21 * f.DAY
    # At equal distance, earlier expiry has precedence over delta preference.
    for row in m["options"].values():
        if row["expiryMs"] == NOW + 21 * f.DAY:
            row["expiryMs"] = NOW + 22 * f.DAY
    picked = f.choose("BTC", m, D(25), P)
    assert picked[1]["expiryMs"] == NOW + 20 * f.DAY


@pytest.mark.parametrize(
    "key,value",
    [
        ("bid", D(0)),
        ("ask", D(20)),
        ("bid", D("NaN")),
        ("bid", None),
        ("bidSize", D(0)),
        ("ask", D("30.05")),
        ("status", "Settled"),
        ("maxPrice", D(20)),
        ("step", D(0)),
    ],
)
def test_bad_quotes_cannot_execute(key, value):
    o = option("BTC", 1000, ".55", "29.9", 30)
    o[key] = value
    assert f.executable(o, "Buy", D(".1"), P, entry=True) is None


def test_quantity_grid_both_steps():
    assert f.common_step(D(".02"), D(".03")) == D(".06")
    m = market()
    for row in m["options"].values():
        row["step"] = D(".02") if row["strike"] == 1000 else D(".03")
    picked = f.choose("BTC", m, D(25), P)
    assert picked[3] % D(".06") == 0


def test_missing_quote_delta_is_not_candidate():
    m = market()
    m["options"]["BTC-TEST-1000-C-USDT"]["delta"] = None
    assert f.choose("BTC", m, D(25), P) is None


def test_stress_budget_and_unreachable_tp_filter():
    m = market()
    assert f.choose("BTC", m, D(".001"), P) is None
    long = m["options"]["BTC-TEST-1000-C-USDT"]
    short = m["options"]["BTC-TEST-1050-C-USDT"]
    # Width 30 means positive payoff, but 50% of debit cannot be reached.
    short["strike"] = D(1030)
    costs = f.economics(long, short, D(1), P)
    assert costs and not costs["gates"]["takeProfitReachable"]
    assert not costs["eligible"]


def test_timeout_no_long_vs_timeout_with_long():
    s, m = state(), market()
    s = f.apply(s, m, P, "first", {})
    no_fill = tick(s, m, seconds=1800)
    assert no_fill["positions"]["BTC"]["phase"] == "cancelled"
    assert no_fill["positions"]["BTC"]["reservedRisk"] == "0"
    s, m = state(), market()
    s = f.apply(s, m, P, "first", {})
    s = tick(s, m)
    s = tick(s, m, seconds=1800)
    assert s["positions"]["BTC"]["phase"] == "aborting"
    assert s["positions"]["BTC"]["longHeld"]
    assert not s["positions"]["BTC"]["shortHeld"]
    s = tick(s, m)
    p = s["positions"]["BTC"]
    assert p["phase"] == "aborted" and not p["longHeld"]
    assert D(p["cash"]) < 0 and D(p["fees"]) > 0
    assert D(p["reservedRisk"]) > 0


def test_actual_long_cash_budget_recheck_aborts_short():
    s, m = state(), market()
    s = f.apply(s, m, P, "first", {})
    s = tick(s, m)
    for row in m["options"].values():
        if row["strike"] == 1050:
            row["bid"], row["ask"] = D(5), D("5.1")
    # Keep original short limit touched to exercise cost check itself.
    for p in s["positions"].values():
        p["pending"]["limit"] = "0"
    s = tick(s, m)
    assert s["positions"]["BTC"]["phase"] == "aborting"
    assert not s["positions"]["BTC"]["shortHeld"]


def test_signal_loss_exit_short_then_long_and_never_same_snapshot():
    s, m = opened()
    for signal in m["signals"].values():
        signal["eligible"] = False
    s = tick(s, m)
    assert s["positions"]["BTC"]["pending"]["purpose"] == "close_short"
    assert s["positions"]["BTC"]["shortHeld"]
    s = tick(s, m)
    assert not s["positions"]["BTC"]["shortHeld"] and s["positions"]["BTC"]["longHeld"]
    s = tick(s, m)
    p = s["positions"]["BTC"]
    assert p["phase"] == "closed" and not p["longHeld"]
    reserved = p["reservedRisk"]
    s = tick(s, m)
    assert s["positions"]["BTC"]["phase"] == "closed"
    assert s["positions"]["BTC"]["reservedRisk"] == reserved


@pytest.mark.parametrize(
    "reason", ["take_profit", "stop_loss", "max_hold", "before_expiry", "study_end_exit"]
)
def test_exit_trigger_is_only_intent(reason):
    s, m = opened()
    p = s["positions"]["BTC"]
    if reason == "max_hold":
        p["openedMs"] = m["ms"] - 72 * f.HOUR
    elif reason == "before_expiry":
        p["expiryMs"] = m["ms"] + 24 * f.HOUR
    elif reason == "study_end_exit":
        s["endMs"] = m["ms"] + 1800 * 1000
    else:
        for row in m["options"].values():
            if row["base"] == "BTC" and row["strike"] == 1000:
                row["bid"] = D(50) if reason == "take_profit" else D(10)
                row["ask"] = row["bid"] + D(".1")
    s = tick(s, m)
    p = s["positions"]["BTC"]
    assert p["exitReason"] == reason
    assert p["shortHeld"] and p["longHeld"]


def test_end_buffer_completes_both_legs_before_cutoff_with_capture_delay():
    s, m = opened()
    s["endMs"] = m["ms"] + 3600 * 1000
    s = tick(s, m, seconds=901)
    assert s["positions"]["BTC"]["phase"] == "closing_short"
    s = tick(s, m, seconds=901)
    assert s["positions"]["BTC"]["phase"] == "closing_long"
    s = tick(s, m, seconds=901)
    assert m["ms"] < s["endMs"]
    assert s["positions"]["BTC"]["phase"] == "closed"


def test_paid_long_is_not_hypothetically_bought_again_in_short_stress():
    s, m = state(), market()
    s = f.apply(s, m, P, "first", {})
    s = tick(s, m)
    reserve = D(s["positions"]["BTC"]["reservedRisk"])
    long = m["options"]["BTC-TEST-1000-C-USDT"]
    short = m["options"]["BTC-TEST-1050-C-USDT"]
    long["bid"], long["ask"] = D("30.9"), D(31)
    short["bid"], short["ask"] = D("10.5"), D("10.6")
    cost = f.economics(long, short, D(1), P, paid_long=-D(s["positions"]["BTC"]["cash"]))
    assert cost["eligible"] and cost["stressDebit"] <= reserve
    assert cost["stressDebit"] == D("20.4")
    s = tick(s, m)
    assert s["positions"]["BTC"]["phase"] == "open"
    assert D(s["positions"]["BTC"]["entryDebit"]) == D("20.3")


def test_no_new_entry_in_final_two_hours_or_incomplete_entry_final_hour():
    s, m = state(), market()
    s["endMs"] = NOW + 2 * f.HOUR
    s = f.apply(s, m, P, "late", {})
    assert all(p["phase"] == "cancelled" for p in s["positions"].values())
    s, m = state(), market()
    s = f.apply(s, m, P, "first", {})
    s = tick(s, m)
    s["endMs"] = m["ms"] + f.HOUR
    s = tick(s, m)
    assert s["positions"]["BTC"]["phase"] == "aborting"
    assert not s["positions"]["BTC"]["shortHeld"]


def test_unknown_missing_quote_then_expiry_no_fabrication():
    s, m = opened()
    p = s["positions"]["BTC"]
    del m["options"][p["long_symbol"]]
    assert f.value(p, m, P) is None
    p["expiryMs"] = m["ms"]
    before = p["cash"]
    s = tick(s, m)
    p = s["positions"]["BTC"]
    assert p["phase"] == "expired_unresolved" and p["cash"] == before
    assert f.value(p, m, P) is None


def test_hold_quotes_outside_entry_window_still_exit():
    s, m = opened()
    p = s["positions"]["BTC"]
    p["expiryMs"] = m["ms"] + 23 * f.HOUR
    for o in m["options"].values():
        o["expiryMs"] = p["expiryMs"]
        o["delta"] = D(".01")
    s = tick(s, m)
    s = tick(s, m)
    s = tick(s, m)
    assert s["positions"]["BTC"]["phase"] == "closed"


def test_gap_and_finish_unresolved(tmp_path):
    s, m = opened()
    s = tick(s, m, seconds=1801)
    assert any(e["kind"] == "observation_gap" for e in s["events"])
    f.finish(s, tmp_path, "test wall-clock bound")
    assert s["positions"]["BTC"]["phase"] == "study_ended_unresolved"
    assert s["positions"]["BTC"]["longHeld"]
    text = (tmp_path / "FINAL_REPORT.md").read_text()
    assert "这些数据可用于模拟定价回测，但不能替代历史期权真实报价。" in text
    assert "未解决敞口" in text


def test_checkpoint_tamper(tmp_path):
    s = state()
    s["checkpointHash"] = f.state_hash(s)
    s["positions"]["BTC"]["cash"] = "1"
    with pytest.raises(f.IntegrityError, match="checksum"):
        f.verify_ledger(s, tmp_path, None)


def test_opposing_state_missing_or_tampered_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(f, "ROOT", tmp_path)
    p = {**P, "opposing_run": "opposite/run-v1"}
    evidence = f.opposite_evidence(p, NOW)
    assert all(evidence["blocked"].values())
    run = tmp_path / p["opposing_run"]
    run.mkdir(parents=True)
    f.atomic_json(run / "state.json", {"checkpointHash": "bad"})
    f.atomic_json(run / "manifest.json", {})
    assert all(f.opposite_evidence(p, NOW)["blocked"].values())


def test_frozen_source_tamper(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("protocol.json", "PROTOCOL.md", "forward.py", "market.py"):
        (source / name).write_text("frozen")
    monkeypatch.setattr(f, "ROOT", source)
    output = tmp_path / "run"
    output.mkdir()
    manifest = f.prepare_run(output, P)
    assert f.prepare_run(output, P) == manifest
    (source / "market.py").write_text("changed")
    with pytest.raises(f.IntegrityError, match="Frozen"):
        f.prepare_run(output, P)


def test_adapter_to_process_serializes_signals_and_verifies_restart(tmp_path, monkeypatch):
    import runpy

    fixtures = runpy.run_path(str(ROOT / "test_market.py"))
    data = fixtures["snapshot"]()
    monkeypatch.setattr(f, "ROOT", tmp_path)
    source = tmp_path / "snapshots"
    source.mkdir()
    path = fixtures["write_snapshot"](source, data)
    output = tmp_path / "run-v1"
    (output / "evidence").mkdir(parents=True)
    manifest = {
        "files": {"protocol.json": "test"},
        "frozenUtc": f.iso(fixtures["CUTOFF"] - 1000),
    }
    module = fixtures["market"]
    monkeypatch.setattr(f.time, "time", lambda: (fixtures["CUTOFF"] + 6000) / 1000)
    monkeypatch.setattr(
        f,
        "opposite_evidence",
        lambda _p, ms: {"checkedMs": ms, "blocked": dict.fromkeys(P["bases"], True)},
    )
    s = f.process(path, P, manifest, output, module)
    assert isinstance(s["samples"][0]["signals"]["BTC"]["sma20"], str)
    assert all(p["phase"] == "waiting" for p in s["positions"].values())
    f.verify_ledger(s, output, module)
    assert f.process(path, P, manifest, output, module) == s
    raw = next(path.parent.glob("request-*/response.json"))
    raw.write_text("{}")
    with pytest.raises(f.IntegrityError, match="raw evidence"):
        f.verify_ledger(s, output, module)
