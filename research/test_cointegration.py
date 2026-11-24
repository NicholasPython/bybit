#!/usr/bin/env python3
"""协整检验自检：口径、误判率与配对分类。

跑的是纯计算与假响应，不需要网络与数据库：

.. code-block:: bash

    python research/test_cointegration.py
"""

from __future__ import annotations

import json
import math
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "cointegration"))

from cointegration import (  # noqa: E402
    ADF_CRITICAL_VALUES,
    ENGLE_GRANGER_CRITICAL_VALUES,
    CriticalValues,
    adf_test,
    approximate_p_value,
    engle_granger_test,
    evaluate_pair,
)
from pool import (  # noqa: E402
    HOUR_MS,
    SymbolQuote,
    _allowed_request,
    aligned_log_prices,
    build_universe,
    evaluate_pool,
    load_hourly_closes,
)

N = 1440


def _walk(rng: random.Random, n: int, drift: float = 0.0002, sigma: float = 0.01) -> list[float]:
    """带漂移的随机游走（对数价格量级）。"""
    out = [10.0]
    for _ in range(n - 1):
        out.append(out[-1] + drift + rng.gauss(0, sigma))
    return out


def _ar1(rng: random.Random, n: int, phi: float, sigma: float = 0.01) -> list[float]:
    """平稳 AR(1) 序列。"""
    out = [0.0]
    for _ in range(n - 1):
        out.append(phi * out[-1] + rng.gauss(0, sigma))
    return out


def test_adf_rejects_stationary_series() -> None:
    """平稳序列必须拒绝单位根。"""
    rng = random.Random(11)
    for _ in range(10):
        result = adf_test(_ar1(rng, N, 0.3), lag=2)
        assert result.stationary, result
        assert result.statistic < result.critical_values.five_percent


def test_adf_keeps_random_walk() -> None:
    """随机游走不能被拒绝：误拒率要接近 5%，不能像含趋势项口径那样高。"""
    rng = random.Random(12)
    rejections = 0
    trials = 100
    for _ in range(trials):
        rejections += adf_test(_walk(rng, N), lag=2).stationary
    assert rejections <= 15, f"随机游走的 ADF 误拒率过高：{rejections}/{trials}"


def test_engle_granger_detects_cointegrated_pair() -> None:
    """协整对必须被识别，β 与半衰期都要能还原。"""
    rng = random.Random(13)
    for _ in range(10):
        x = _walk(rng, N)
        error = _ar1(rng, N, 0.7)
        y = [1.5 * xi + ei for xi, ei in zip(x, error, strict=True)]
        result = engle_granger_test(y, x, lag=2)
        assert result.cointegrated, result
        assert abs(result.hedge_ratio - 1.5) < 0.05, result.hedge_ratio
        assert result.half_life is not None
        expected = -math.log(2.0) / math.log(0.7)
        assert abs(result.half_life - expected) < 1.0, (result.half_life, expected)


def test_engle_granger_false_positive_rate_is_bounded() -> None:
    """两条独立随机游走：误判（判定协整）比例必须接近 5%。"""
    rng = random.Random(14)
    false_positives = 0
    trials = 100
    for _ in range(trials):
        y = _walk(rng, N)
        x = _walk(rng, N)
        false_positives += engle_granger_test(y, x, lag=2).cointegrated
    assert false_positives <= 15, f"独立随机游走的协整误判过高：{false_positives}/{trials}"


def test_p_value_is_anchored_on_critical_values() -> None:
    """p 值插值必须锚在临界值上：5% 点给 0.05，10% 点给 0.10。"""
    for table in (ADF_CRITICAL_VALUES, ENGLE_GRANGER_CRITICAL_VALUES):
        critical = CriticalValues(*table)
        assert abs(approximate_p_value(critical.five_percent, critical) - 0.05) < 1e-6
        assert abs(approximate_p_value(critical.ten_percent, critical) - 0.10) < 1e-6
        assert approximate_p_value(critical.one_percent, critical) < 0.02
        assert approximate_p_value(0.0, critical) > 0.5


def test_evaluate_pair_reports_stability_and_integration_order() -> None:
    """评估结果要带上两腿 I(1) 与前后半段的稳定性。"""
    rng = random.Random(15)
    x = _walk(rng, N)
    error = _ar1(rng, N, 0.7)
    y = [1.5 * xi + ei for xi, ei in zip(x, error, strict=True)]
    evaluation = evaluate_pair("BTCUSDT", x, "ETHUSDT", y, lag=2)
    assert evaluation.both_integration_order_one
    assert evaluation.halves == 2
    assert evaluation.half_rejections == 2
    assert evaluation.stable


def test_evaluate_pair_flags_unstable_relation() -> None:
    """协整只在前半段成立时，整段结论不能算稳定。

    后半段把价差换成随机游走（关系断了但两腿仍是 I(1)），这时 EG 在后半段
    不会拒绝不协整，稳定性检查必须把整段判成不稳。
    """
    rng = random.Random(16)
    x = _walk(rng, N)
    error = _ar1(rng, N, 0.7)
    middle = N // 2
    drift = _walk(rng, N - middle)
    y = [1.5 * xi + ei for xi, ei in zip(x[:middle], error[:middle], strict=True)]
    y += [1.5 * xi + di for xi, di in zip(x[middle:], drift, strict=True)]
    evaluation = evaluate_pair("BTCUSDT", x, "ETHUSDT", y, lag=2)
    assert evaluation.half_rejections == 1, evaluation
    assert not evaluation.stable


def test_allowed_request_blocks_everything_outside_public_linear_reads() -> None:
    """白名单只放行 linear 公共只读请求。"""
    for path, params in (
        ("/v5/market/tickers", {"category": "spot"}),
        ("/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"}),
        ("/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "1",
                              "start": 0, "end": 1, "limit": 1000}),
        ("/v5/market/kline", {"category": "linear", "symbol": "btcusdt", "interval": "60",
                              "start": 0, "end": 1, "limit": 1000}),
        ("/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "60",
                              "start": 0, "end": 1, "limit": 500}),
        ("/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "60",
                              "start": 10, "end": 1, "limit": 1000}),
        ("/v5/market/account/wallet-balance", {"category": "linear"}),
        ("/v5/market/order/place", {"category": "linear"}),
    ):
        try:
            _allowed_request(path, params)
        except ValueError:
            continue
        raise AssertionError(f"白名单放行了不该放行的请求：{path} {params}")


def _envelope(payloads: dict[tuple[str, str], object]) -> dict[str, object]:
    """把 ``(path, 参数摘要) -> result.list`` 拼成采集信封。"""
    records = []
    for (path, key), rows in payloads.items():
        records.append(
            {
                "path": path,
                "params": json.loads(key.split("|", 1)[1]),
                "payload": {"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}},
            }
        )
    return {"records": records}


def test_build_universe_ranks_by_turnover_and_filters_contracts() -> None:
    """池子按成交额排序，并剔除非永续与非交易状态的合约。"""
    tickers = [
        {"symbol": "AAAUSDT", "turnover24h": "10"},
        {"symbol": "BBBUSDT", "turnover24h": "30"},
        {"symbol": "CCCUSDT", "turnover24h": "20"},
        {"symbol": "DDDUSDT", "turnover24h": "40"},
    ]
    instruments = [
        {"symbol": "AAAUSDT", "contractType": "LinearPerpetual", "status": "Trading",
         "baseCoin": "AAA"},
        {"symbol": "BBBUSDT", "contractType": "LinearFutures", "status": "Trading",
         "baseCoin": "BBB"},
        {"symbol": "CCCUSDT", "contractType": "LinearPerpetual", "status": "Trading",
         "baseCoin": "CCC"},
        {"symbol": "DDDUSDT", "contractType": "LinearPerpetual", "status": "Closed",
         "baseCoin": "DDD"},
    ]

    def fetch(jobs):
        payloads = {}
        for path, params in jobs:
            key = f"{path}|{json.dumps(params, sort_keys=True)}"
            if path == "/v5/market/tickers":
                payloads[(path, key)] = tickers
            else:
                payloads[(path, key)] = instruments
        return _envelope(payloads)

    quotes, skipped = build_universe(fetch, top=10)
    assert [quote.symbol for quote in quotes] == ["CCCUSDT", "AAAUSDT"]
    assert quotes[0].turnover_24h == 20.0
    assert any("不是永续" in item for item in skipped)
    assert any("状态不是 Trading" in item for item in skipped)


def test_load_hourly_closes_drops_unfinished_bar_and_thin_cover() -> None:
    """未收盘的那一根不参与统计；覆盖不足的合约要被剔除并留痕。"""
    end = 100 * HOUR_MS
    start = end - 10 * HOUR_MS
    stamps = [start + index * HOUR_MS for index in range(10)]
    rows = [
        [stamp, "1", "1", "1", f"{100 + index}", "1", "1"]
        for index, stamp in enumerate(stamps)
    ]
    # 多一行"当前未收盘"的 K 线：start 落在窗口内但还没走完一个小时。
    rows.append([end, "1", "1", "1", "999", "1", "1"])
    thin = [[stamp, "1", "1", "1", "100", "1", "1"] for stamp in stamps[:3]]

    def rows_for(symbol):
        return thin if symbol == "THINUSDT" else rows

    payloads = {}
    for symbol in ("FULLUSDT", "THINUSDT"):
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": "60",
            "start": start,
            "end": end,
            "limit": 1000,
        }
        key = f"/v5/market/kline|{json.dumps(params, sort_keys=True)}"
        payloads[("/v5/market/kline", key)] = rows_for(symbol)
    closes, dropped = load_hourly_closes(
        _envelope(payloads), ["FULLUSDT", "THINUSDT"], start_ms=start, end_ms=end
    )
    assert set(closes) == {"FULLUSDT"}
    assert len(closes["FULLUSDT"]) == 10
    assert end not in closes["FULLUSDT"]
    assert any("覆盖不足" in item for item in dropped)


def test_aligned_log_prices_intersects_timestamps() -> None:
    """对齐只取两条序列共有的时间戳。"""
    a = {1: 100.0, 2: 110.0, 3: 120.0}
    b = {2: 2.0, 3: 3.0}
    left, right = aligned_log_prices(a, b)
    assert len(left) == 2 and len(right) == 2
    assert abs(left[0] - math.log(110.0)) < 1e-9
    assert abs(right[0] - math.log(2.0)) < 1e-9


def test_evaluate_pool_classifies_and_nominates() -> None:
    """在册配对按结论归档；同一标的的两个合约要跳过；合格的配对才提名。"""
    rng = random.Random(17)
    stamps = [index * HOUR_MS for index in range(N)]
    base = _walk(rng, N)
    error = _ar1(rng, N, 0.7)
    cointegrated = [value + 0.3 * noise for value, noise in zip(base, error, strict=True)]
    independent = _walk(rng, N)
    quotes = [
        SymbolQuote(symbol="AAAUSDT", turnover_24h=30.0, contract_type="LinearPerpetual",
                    status="Trading", base_coin="AAA"),
        SymbolQuote(symbol="BBBUSDT", turnover_24h=20.0, contract_type="LinearPerpetual",
                    status="Trading", base_coin="BBB"),
        SymbolQuote(symbol="CCCUSDT", turnover_24h=10.0, contract_type="LinearPerpetual",
                    status="Trading", base_coin="CCC"),
        SymbolQuote(symbol="AAAPERP", turnover_24h=5.0, contract_type="LinearPerpetual",
                    status="Trading", base_coin="AAA"),
    ]
    closes = {
        "AAAUSDT": dict(zip(stamps, base, strict=True)),
        "BBBUSDT": dict(zip(stamps, cointegrated, strict=True)),
        "CCCUSDT": dict(zip(stamps, independent, strict=True)),
        "AAAPERP": dict(zip(stamps, base, strict=True)),
    }
    results, skipped, others = evaluate_pool(
        quotes,
        closes,
        configured_pairs=["AAAUSDT:CCCUSDT"],
        min_observations=500,
        adf_lag=2,
    )
    classification = {item.evaluation.symbol_a + ":" + item.evaluation.symbol_b: item.classification
                      for item in results}
    assert classification["AAAUSDT:CCCUSDT"] == "drop"
    assert classification["AAAUSDT:BBBUSDT"] == "nominate"
    assert any("同一标的" in item for item in skipped)
    assert others >= 0


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} 项自检通过")


if __name__ == "__main__":
    main()
