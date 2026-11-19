"""现货－永续价差采集与回测的离线回归。

所有公开响应都是合成数据：不加载凭据、不访问交易所、不修改产品交易状态。
"""

from __future__ import annotations

import hashlib
import json
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

from cash_futures import (  # noqa: E402
    Candle,
    ContractSpec,
    CostModel,
    FundingPoint,
    HistoryData,
    StrategyConfig,
    _allowed_request,
    _annualized_funding,
    _latest_funding,
    _top_from_book,
    collect_live,
    live_spread,
    load_history,
    normalize_live_snapshot,
    run_backtest,
    save_report,
    scan_opportunity,
)


def _jobs() -> list[tuple[str, dict[str, object]]]:
    jobs: list[tuple[str, dict[str, object]]] = [("/v5/market/time", {})]
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for category in ("spot", "linear"):
            jobs += [
                ("/v5/market/orderbook", {"category": category, "symbol": symbol, "limit": 25}),
                ("/v5/market/tickers", {"category": category, "symbol": symbol}),
                ("/v5/market/instruments-info", {"category": category, "symbol": symbol}),
            ]
    return jobs


def _quote(category: str, base: str) -> dict:
    offset = D("-5") if category == "linear" else D("0")
    price = D("100") + offset if base == "BTCUSDT" else D("10") + offset
    return {
        "bids": [[str(price - D("0.1")), "2"]],
        "asks": [[str(price + D("0.1")), "2"]],
        "ts": "1000",
    }


def _payload(path: str, params: dict) -> dict:
    symbol = params.get("symbol", "BTCUSDT")
    if path == "/v5/market/time":
        return {"retCode": 0, "time": "1000", "result": {"time": "1000"}}
    if path == "/v5/market/orderbook":
        return {"retCode": 0, "result": _quote(params["category"], symbol)}
    if path == "/v5/market/tickers":
        quote = _quote(params["category"], symbol)
        return {
            "retCode": 0,
            "time": "1000",
            "result": {
                "list": [
                    {
                        "bid1Price": quote["bids"][0][0],
                        "ask1Price": quote["asks"][0][0],
                        "bid1Size": quote["bids"][0][1],
                        "ask1Size": quote["asks"][0][1],
                    }
                ]
            },
        }
    return {
        "retCode": 0,
        "result": {
            "list": [
                {
                    "symbol": symbol,
                    "priceFilter": {"tickSize": "0.01"},
                    "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
                }
            ]
        },
    }


def test_allowlist_rejects_unknown_or_unsafe_requests():
    with pytest.raises(ValueError, match="allowlist"):
        _allowed_request("/v5/account/wallet-balance", {})
    with pytest.raises(ValueError):
        _allowed_request("/v5/market/orderbook", {"category": "linear", "symbol": "SOLUSDT"})
    with pytest.raises(ValueError):
        _allowed_request("/v5/market/time", {"extra": "value"})
    with pytest.raises(ValueError):
        _allowed_request(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": "BTCUSDT", "startTime": 5, "endTime": 3},
        )
    assert _allowed_request(
        "/v5/market/orderbook", {"category": "spot", "symbol": "BTCUSDT", "limit": 25}
    )


def test_live_snapshot_verification_and_spread(tmp_path: Path, monkeypatch):
    jobs = _jobs()
    records = [
        {
            "path": path,
            "params": params,
            "receivedUtc": "2026-11-18T00:00:01+00:00",
            "payload": _payload(path, params),
        }
        for path, params in jobs
    ]
    envelope = {
        "startedUtc": "2026-11-18T00:00:00+00:00",
        "completedUtc": "2026-11-18T00:00:02+00:00",
        "records": records,
    }
    monkeypatch.setattr("cash_futures.fetch_public", lambda *a, **k: envelope)
    manifest = collect_live(tmp_path, ssh_host="unused")
    view = normalize_live_snapshot(manifest)
    spread = live_spread(view, "BTCUSDT")
    assert spread["sourceMs"] == 1000
    assert spread["entryBps"] > 0
    assert spread["spotAsk"] == D("100.1")
    assert spread["perpBid"] == D("94.9")
    assert spread["exitBps"] < 0
    tampered = json.loads(manifest.read_text())
    raw = manifest.parent / tampered["records"][1]["file"]
    raw.write_text(raw.read_text() + " ")
    with pytest.raises(ValueError, match="digest"):
        normalize_live_snapshot(manifest)


def test_top_of_book_rejects_crossed_and_empty():
    with pytest.raises(ValueError):
        _top_from_book({"result": {"bids": [], "asks": [["1", "1"]]}}, 1)
    with pytest.raises(ValueError):
        _top_from_book({"result": {"bids": [["2", "1"]], "asks": [["1", "1"]]}}, 1)


def _kline_rows(start: int, bars: int, width: int, premium: D) -> list[list[str]]:
    rows = []
    for index in range(bars):
        stamp = start + index * width
        base = D("100")
        close = base + premium
        rows.append(
            [str(stamp), str(base), str(close), str(base - D("1")), str(close), "100", "10000"]
        )
    return rows


def _spec() -> dict:
    return {
        "spot:BTCUSDT": {"tick_size": "0.01", "qty_step": "0.001", "min_qty": "0.001"},
        "linear:BTCUSDT": {"tick_size": "0.01", "qty_step": "0.001", "min_qty": "0.001"},
        "spot:ETHUSDT": {"tick_size": "0.01", "qty_step": "0.001", "min_qty": "0.001"},
        "linear:ETHUSDT": {"tick_size": "0.01", "qty_step": "0.001", "min_qty": "0.001"},
    }


def test_history_loader_rejects_bad_digest_and_gap(tmp_path: Path):
    start, width, bars = 0, 3_600_000, 10
    files = {}
    for category in ("spot", "linear"):
        for symbol in ("BTCUSDT", "ETHUSDT"):
            name = f"kline-{category}-{symbol}.csv"
            content = "timestamp,open,high,low,close\n" + "".join(
                f"{start + i * width},100,101,99,100\n" for i in range(bars)
            )
            (tmp_path / name).write_text(content)
            files[name] = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    for symbol in ("BTCUSDT", "ETHUSDT"):
        name = f"funding-{symbol}.csv"
        (tmp_path / name).write_text(f"timestamp,rate\n{start + width},0.0001\n")
        files[name] = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    (tmp_path / "specs.json").write_text(json.dumps(_spec()))
    files["specs.json"] = hashlib.sha256((tmp_path / "specs.json").read_bytes()).hexdigest()
    manifest = {
        "kind": "cash-futures-history",
        "startMs": start,
        "endMs": start + bars * width,
        "intervalMs": width,
        "files": files,
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    data = load_history(tmp_path / "manifest.json")
    assert len(data.candle("spot", "BTCUSDT")) == bars
    (tmp_path / "kline-spot-BTCUSDT.csv").write_text("timestamp,open,high,low,close\n")
    with pytest.raises(ValueError, match="digest"):
        load_history(tmp_path / "manifest.json")


def _candles(
    start: int,
    bars: int,
    width: int,
    *,
    premium: D,
) -> list[Candle]:
    rows = []
    for index in range(bars):
        stamp = start + index * width
        open_ = D("100") + premium
        rows.append(Candle(stamp, open_, open_ + D("1"), open_ - D("1"), open_))
    return rows


def _history(
    start: int,
    bars: int,
    width: int,
    *,
    perp_premium: D,
    funding: list[FundingPoint] | None = None,
) -> HistoryData:
    candles = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        candles[("spot", symbol)] = _candles(start, bars, width, premium=D("0"))
        candles[("linear", symbol)] = _candles(start, bars, width, premium=perp_premium)
    points = funding if funding is not None else []
    return HistoryData(
        start,
        start + bars * width,
        width,
        candles,
        {symbol: list(points) for symbol in ("BTCUSDT", "ETHUSDT")},
        {name: ContractSpec(D("0.01"), D("0.001"), D("0.001")) for name in _spec()},
    )


def test_backtest_trades_and_costs():
    start, width = 0, 3_600_000
    bars = 200
    data = _history(
        start,
        bars,
        width,
        perp_premium=D("0.5"),
        funding=[FundingPoint(start + 3 * width, D("0.001"))],
    )
    strategy = StrategyConfig(
        version="test-v1",
        entry_bps=D("20"),
        exit_bps=D("1"),
        funding_hold_bars=2,
        max_hold_bars=10,
        capital_per_symbol=D("100"),
    )
    costs = CostModel(version="test-cost-v1", spread_slippage_bps=D("0"), stress_multiplier=D("0"))
    result = run_backtest(data, strategy, costs)
    assert result.trades
    assert all(t.reason in ("funding", "time", "stop", "eod") for t in result.trades)
    assert result.metrics()["funding_received"] > 0
    assert result.metrics()["fees"] > 0
    assert result.metrics()["net_pnl"] == sum(t.pnl for t in result.trades)
    assert all(t.exit_time > t.entry_time for t in result.trades)
    assert len(result.equity) == bars + 1
    assert result.equity[-1][1] == result.metrics()["net_pnl"]


def test_short_perp_receives_positive_funding_and_short_spot_pays():
    start, width = 0, 3_600_000
    bars = 60
    funding = [FundingPoint(start + 3 * width, D("0.001"))]
    costs = CostModel(version="c", spread_slippage_bps=D("0"), stress_multiplier=D("0"))
    short_perp = run_backtest(
        _history(start, bars, width, perp_premium=D("0.5"), funding=funding),
        StrategyConfig(
            version="v1", direction="short_perp", entry_bps=D("20"), exit_bps=D("1"),
            funding_hold_bars=2, max_hold_bars=10,
        ),
        costs,
    )
    short_spot = run_backtest(
        _history(start, bars, width, perp_premium=D("-0.5"), funding=funding),
        StrategyConfig(
            version="v1", direction="short_spot", entry_bps=D("20"), exit_bps=D("1"),
            funding_hold_bars=2, max_hold_bars=10,
        ),
        costs,
    )
    assert short_perp.trades and short_spot.trades
    assert short_perp.metrics()["funding_received"] > 0
    assert short_spot.metrics()["funding_received"] < 0


def test_funding_capture_requires_settled_funding_and_records_rejections():
    start, width = 0, 3_600_000
    bars = 60
    # 资金费低于年化门槛时不应开仓。
    low = run_backtest(
        _history(start, bars, width, perp_premium=D("-0.2"), funding=[]),
        StrategyConfig(
            version="v1", mode="funding_capture", funding_entry_annual_pct=D("0.03"),
            funding_hold_bars=2, max_hold_bars=10,
        ),
        CostModel(version="c"),
    )
    assert not low.trades
    assert low.rejected["no_settled_funding"] > 0
    # 资金费足够高时开仓。
    high = run_backtest(
        _history(
            start, bars, width, perp_premium=D("-0.2"),
            funding=[FundingPoint(start + 3 * width, D("0.01"))],
        ),
        StrategyConfig(
            version="v1", mode="funding_capture", funding_entry_annual_pct=D("0.03"),
            funding_hold_bars=2, max_hold_bars=10,
        ),
        CostModel(version="c", spread_slippage_bps=D("0"), stress_multiplier=D("0")),
    )
    assert high.trades
    assert high.rejected["funding_too_low"] >= 0


def test_latest_funding_only_returns_settled_points():
    points = [FundingPoint(0, D("0.001")), FundingPoint(10_000, D("0.002"))]
    assert _latest_funding(points, 5_000).timestamp == 0
    assert _latest_funding(points, 10_000).timestamp == 10_000
    assert _latest_funding(points, -1) is None
    assert _annualized_funding(D("0.0001"), 8 * 3_600_000) == D("0.1095")


def test_scan_reports_hurdle_and_basis_distribution():
    start, width = 0, 3_600_000
    bars = 100
    data = _history(start, bars, width, perp_premium=D("-0.4"))
    report = scan_opportunity(data, CostModel(version="c"))
    assert report["roundtrip_fee_bps"] == D("31.0")
    assert report["hurdle_bps"] > report["roundtrip_fee_bps"]
    assert report["symbols"][0]["basis_max_bps"] < 0
    assert report["symbols"][0]["bars_over_hurdle"] == 0
    reverse = scan_opportunity(data, CostModel(version="c"), direction="short_spot")
    assert reverse["symbols"][0]["edge_bps"] > 0


def test_no_trade_when_spread_below_threshold():
    start, width, bars = 0, 3_600_000, 30
    data = _history(start, bars, width, perp_premium=D("0.01"))
    result = run_backtest(
        data,
        StrategyConfig(
            version="test-v1",
            entry_bps=D("20"),
            exit_bps=D("1"),
            funding_hold_bars=2,
            max_hold_bars=10,
        ),
        CostModel(version="test-cost-v1"),
    )
    assert not result.trades
    assert result.rejected["below_entry"] > 0


def test_save_report_serializes_decimals(tmp_path: Path):
    start, width, bars = 0, 3_600_000, 200
    data = _history(
        start,
        bars,
        width,
        perp_premium=D("1"),
        funding=[FundingPoint(start + 3 * width, D("0.001"))],
    )
    result = run_backtest(data, StrategyConfig(version="v1"), CostModel(version="v1"))
    path = tmp_path / "report.json"
    save_report(result, path)
    payload = json.loads(path.read_text())
    assert payload["metrics"]["trades"] == len(result.trades)
    assert isinstance(payload["equity"][0][1], str)


def test_open_position_is_marked_to_market_at_sample_end():
    """样本末尾仍持仓时必须盯市平掉，不能静默丢掉未结算的资金费与平仓费。"""
    start, width, bars = 0, 3_600_000, 50
    data = _history(
        start,
        bars,
        width,
        perp_premium=D("0.5"),
        funding=[FundingPoint(start + 3 * width, D("0.001"))],
    )
    # 最长持有取 bars-2：持有期内不会触发时间平仓，结束时仓位还开着。
    result = run_backtest(
        data,
        StrategyConfig(
            version="v1",
            entry_bps=D("20"),
            exit_bps=D("1"),
            funding_hold_bars=2,
            max_hold_bars=bars - 2,
        ),
        CostModel(version="c", spread_slippage_bps=D("0"), stress_multiplier=D("0")),
    )
    assert len(result.trades) == 2  # 每个标的一条
    for trade in result.trades:
        assert trade.reason == "eod"
        assert trade.exit_time == data.end
        # 盯市平仓要计入平仓手续费与区间内的资金费。
        assert trade.spot_fees > 0 and trade.perp_fees > 0
        assert trade.funding > 0
        assert trade.funding_periods == 1
    assert result.metrics()["net_pnl"] == sum(t.pnl for t in result.trades)


def test_funding_exit_threshold_may_be_negative_to_hold_through_negative_carry():
    # 平仓阈值取负值表示「扛过负 carry」，这会显著改变换手率。
    strategy = StrategyConfig(
        version="v1",
        mode="funding_capture",
        funding_exit_annual_pct=D("-1"),
        funding_entry_annual_pct=D("0.05"),
    )
    assert strategy.funding_exit_annual_pct == D("-1")
    with pytest.raises(ValueError, match="funding_exit_annual_pct"):
        StrategyConfig(version="v1", funding_exit_annual_pct=D("-2"))
    with pytest.raises(ValueError, match="funding_entry_annual_pct"):
        StrategyConfig(version="v1", funding_entry_annual_pct=D("-0.1"))
