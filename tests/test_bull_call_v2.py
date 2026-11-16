"""Shared v2 parity, immutable dependency closure and protected original studies."""

from __future__ import annotations

import copy
import importlib.util
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parents[1] / "research"
V2 = RESEARCH / "2026-11-15-bull-call-forward-v2"
V1 = RESEARCH / "2026-11-15-bull-call-forward"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


f = module("bull_shared_v2_test", V2 / "forward.py")
old = module("bull_frozen_v1_test", V1 / "forward.py")
market_v2 = module("bull_market_v2_test", V2 / "market.py")
market_v1 = module("bull_market_v1_test", V1 / "market.py")
P = json.loads((V2 / "protocol.json").read_text())
OLD_P = json.loads((V1 / "protocol.json").read_text())
NOW = 1_794_744_000_000


def market():
    options = {}
    for base in P["bases"]:
        for strike, delta, bid, ask in ((1000, ".55", "29.9", "30"), (1050, ".35", "10", "10.1")):
            symbol = f"{base}-TEST-{strike}-C-USDT"
            options[symbol] = {
                "symbol": symbol,
                "base": base,
                "status": "Trading",
                "expiryMs": NOW + 21 * f.DAY,
                "strike": D(strike),
                "delta": D(delta),
                "index": D(1000),
                "bid": D(bid),
                "ask": D(ask),
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
    return {
        "ms": NOW,
        "options": options,
        "signals": dict.fromkeys(P["bases"], {"eligible": True}),
        "conflicts": dict.fromkeys(P["bases"], False),
    }


def state(engine=f, protocol=P):
    return engine.new_state(
        protocol,
        {
            "files": {"protocol.json": "synthetic"},
            "frozenUtc": f.iso(NOW - 1000),
        },
    )


def advance(s, m, *, seconds=900, fresh=True, engine=f, protocol=P):
    m["ms"] += seconds * 1000
    if fresh:
        for quote in m["options"].values():
            quote["sourceMs"] = m["ms"]
    return engine.apply(s, m, protocol, str(m["ms"]), {})


@pytest.mark.parametrize("side", ["Buy", "Sell"])
def test_shared_fee_precision_and_liquidation_match_valid_v1(side):
    quote = market()["options"]["BTC-TEST-1000-C-USDT"]
    for qty in (D(".01"), D(".3"), D(1)):
        assert f.fee(quote, D("30.1"), qty, P) == old.fee(quote, D("30.1"), qty, OLD_P)
    assert f.limit_price(quote, side, P) == old.limit_price(quote, side, OLD_P)
    assert f.common_step(D(".02"), D(".03")) == old.common_step(D(".02"), D(".03"))
    m, s = market(), state()
    s = f.apply(s, m, P, "first", {})
    s = advance(s, m)
    s = advance(s, m)
    p = s["positions"]["BTC"]
    assert f.value(p, m, P) == old.value(p, m, OLD_P)
    missing = copy.deepcopy(m)
    missing["options"].pop(p["long_symbol"])
    assert f.value(p, missing, P) is None


def test_strategy_signal_matches_frozen_v1():
    cutoff = NOW // f.DAY * f.DAY

    def record(width, count):
        return {
            "params": {"end": cutoff},
            "payload": {
                "result": {
                    "list": [
                        [
                            str(cutoff - (count - i) * width),
                            str(900 + i),
                            str(901 + i),
                            str(899 + i),
                            str(900 + i),
                            "1",
                            "100",
                        ]
                        for i in range(count)
                    ]
                }
            },
        }

    hour_record, day_record = record(f.HOUR, 25), record(f.DAY, 51)
    hours = market_v1._candles(hour_record, cutoff, f.HOUR, 25)
    days = market_v1._candles(day_record, cutoff, f.DAY, 51)
    assert market_v2.bull_signal(hours, days) == market_v1._signal(hour_record, day_record, cutoff)


def test_v1_v2_state_machine_cash_fees_and_events_match():
    m = market()
    a, b = state(old, OLD_P), state()
    for number in range(7):
        if number == 3:
            m["signals"] = dict.fromkeys(P["bases"], {"eligible": False})
        a = old.apply(a, m, OLD_P, str(number), {})
        b = f.apply(b, m, P, str(number), {})
        for key in ("positions", "events", "samples", "lastMs", "processed"):
            assert a[key] == b[key]
        m["ms"] += 900_000
        for quote in m["options"].values():
            quote["sourceMs"] = m["ms"]
    assert b["positions"]["BTC"]["phase"] == "closed"


def test_next_independent_quote_required_for_each_entry_and_exit_leg():
    m, s = market(), state()
    s = f.apply(s, m, P, "initial", {})
    assert not any(e["kind"] == "simulated_fill" for e in s["events"])
    s = advance(s, m, fresh=False)
    assert not s["positions"]["BTC"]["longHeld"]
    s = advance(s, m, seconds=1)
    p = s["positions"]["BTC"]
    assert p["longHeld"] and not p["shortHeld"]
    s = advance(s, m)
    assert s["positions"]["BTC"]["phase"] == "open"
    m["signals"] = dict.fromkeys(P["bases"], {"eligible": False})
    s = advance(s, m)
    assert s["positions"]["BTC"]["shortHeld"]
    s = advance(s, m)
    assert not s["positions"]["BTC"]["shortHeld"]
    assert s["positions"]["BTC"]["longHeld"]
    s = advance(s, m)
    assert s["positions"]["BTC"]["phase"] == "closed"
    fills = [e for e in s["events"] if e["kind"] == "simulated_fill"]
    assert all(e["quoteSourceMs"] > e["decisionSourceMs"] for e in fills)
    assert sum(D(p["reservedRisk"]) for p in s["positions"].values()) <= 25


@pytest.mark.parametrize("long_filled", [False, True])
def test_timeout_preserves_filled_long_until_later_rollback(long_filled):
    m, s = market(), state()
    s = f.apply(s, m, P, "first", {})
    if long_filled:
        s = advance(s, m)
    s = advance(s, m, seconds=1800)
    p = s["positions"]["BTC"]
    assert not p["shortHeld"]
    if long_filled:
        assert p["phase"] == "aborting" and p["longHeld"]
        s = advance(s, m)
        p = s["positions"]["BTC"]
        assert p["phase"] == "aborted" and not p["longHeld"]
        assert D(p["cash"]) < 0 and D(p["fees"]) > 0
    else:
        assert p["phase"] == "cancelled" and p["reservedRisk"] == "0"


@pytest.fixture
def isolated_sources(tmp_path, monkeypatch):
    sources = f.source_files()
    root = tmp_path / "engine"
    package = tmp_path / "package/bybitbot"
    for name, source in sources.items():
        target = (
            package / Path(name).relative_to("shared/bybitbot")
            if name.startswith("shared/")
            else root / name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    monkeypatch.setattr(f, "ROOT", root)
    monkeypatch.setattr(f, "PACKAGE_ROOT", package)
    return root, package, tmp_path / "run-v2"


def test_freeze_contains_recursive_sources_and_all_package_initializers(isolated_sources):
    _, _, output = isolated_sources
    manifest = f.prepare_run(output, P)
    expected = {
        "protocol.json",
        "PROTOCOL.md",
        "forward.py",
        "market.py",
        "shared/bybitbot/__init__.py",
        "shared/bybitbot/money.py",
        "shared/bybitbot/terms/__init__.py",
        "shared/bybitbot/terms/fees.py",
        "shared/bybitbot/terms/specs.py",
        "shared/bybitbot/terms/symbols.py",
        "shared/bybitbot/terms/valuation.py",
        "shared/bybitbot/marketdata/__init__.py",
        "shared/bybitbot/marketdata/snapshots.py",
    }
    assert set(manifest["files"]) == expected
    for name, sha in manifest["files"].items():
        assert f.digest(output / "frozen" / name) == sha
    assert f.prepare_run(output, P) == manifest


@pytest.mark.parametrize("target", ["source", "archive", "missing", "new_dependency"])
def test_shared_source_and_archive_tampering_reject_resume(isolated_sources, target):
    _, package, output = isolated_sources
    f.prepare_run(output, P)
    if target == "source":
        file = package / "terms/fees.py"
        file.write_text(file.read_text() + "\n# changed\n")
    elif target == "new_dependency":
        file = package / "terms/fees.py"
        file.write_text(file.read_text() + "\nimport bybitbot.new_dependency\n")
        (package / "new_dependency.py").write_text("VALUE = 1\n")
    elif target == "missing":
        (output / "frozen/shared/bybitbot/terms/fees.py").unlink()
    else:
        (output / "frozen/shared/bybitbot/terms/fees.py").write_text("# tampered\n")
    with pytest.raises(f.IntegrityError, match="Frozen source"):
        f.prepare_run(output, P)


def test_recursive_added_dependency_is_frozen_on_first_run(isolated_sources):
    _, package, output = isolated_sources
    file = package / "terms/fees.py"
    file.write_text(file.read_text() + "\nimport bybitbot.extra\n")
    (package / "extra.py").write_text("from bybitbot.nested import VALUE\n")
    (package / "nested.py").write_text("VALUE = 1\n")
    manifest = f.prepare_run(output, P)
    assert "shared/bybitbot/extra.py" in manifest["files"]
    assert "shared/bybitbot/nested.py" in manifest["files"]


@pytest.mark.parametrize("study", ["2026-11-15-bull-call-forward", "2026-11-15-options-forward"])
def test_original_run_and_symlink_alias_rejected_before_any_write(tmp_path, study):
    original = RESEARCH / study
    destination = original / "run-v1/v2-must-not-be-created"
    assert not destination.exists()
    alias = tmp_path / "old-study"
    alias.symlink_to(original, target_is_directory=True)
    for path in (destination, alias / "run-v1/v2-must-not-be-created", original / "run-v1"):
        with pytest.raises(f.IntegrityError, match="protected"):
            f.prepare_run(path, P)
    assert not destination.exists()


def test_main_rejects_original_run_before_lock_open(monkeypatch):
    old_lock = V1 / "run-v1/runner.lock"
    before = old_lock.read_bytes() if old_lock.exists() else None
    monkeypatch.setattr("sys.argv", ["forward.py", "--run-dir", str(V1 / "run-v1")])
    with pytest.raises(f.IntegrityError, match="protected"):
        f.main()
    assert (old_lock.read_bytes() if old_lock.exists() else None) == before


def test_existing_run_with_missing_manifest_or_internal_symlink_rejected(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    (output / "state.json").write_text("{}")
    with pytest.raises(f.IntegrityError, match="Manifest missing"):
        f.prepare_run(output, P)
    (output / "state.json").unlink()
    (output / "runner.lock").symlink_to(tmp_path / "outside")
    with pytest.raises(f.IntegrityError, match="symlinks"):
        f.prepare_run(output, P)
    assert not (tmp_path / "outside").exists()
