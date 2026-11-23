"""FR-07.3 影子盘：公开行情、深度模拟、无真实订单与只读投影测试。"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from bybitbot.adapters.base import InstrumentSpec, OrderBook, Ticker
from bybitbot.clock import now_utc
from bybitbot.settings.loader import load_config
from bybitbot.settings.models import SpreadSettings
from bybitbot.spread.models import ShadowTrade, SpreadSignalSample
from bybitbot.spread.shadow import ENVIRONMENT, ShadowEngine

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/config.example.yaml"


def config(*, enabled=True):
    """spread.enabled=False 表示套利子系统关闭；影子盘独立于该开关采样。"""
    return SpreadSettings.model_validate(
        {
            "enabled": False,
            "pairs": [
                {
                    "pair_key": "BTCUSDT:ETHUSDT",
                    "symbol_a": "BTCUSDT",
                    "symbol_b": "ETHUSDT",
                    "hedge_ratio": "1",
                    "z_entry": "1",
                    "z_exit": "0.2",
                    "z_stop": "3",
                    "max_notional": {"value": "300", "currency": "USDT"},
                    "max_unhedged_notional": {"value": "20", "currency": "USDT"},
                    "enabled": False,
                }
            ],
            "auto": {
                "enabled": True,
                "window": 5,
                "sample_interval_seconds": 2,
                "fee_rate": "0",
                "slippage_bps": "0",
                "target_notional": "100",
            },
            "shadow": {
                "enabled": True,
                "observation_days": 7,
                "min_depth_notional": "10",
                "impact_bps_per_depth": "0",
            },
        }
    )


def specs():
    return {
        symbol: InstrumentSpec(
            "linear",
            symbol,
            symbol[:-4],
            "USDT",
            "Trading",
            settle_coin="USDT",
            tick_size=D("0.01"),
            qty_step=D("0.01"),
        )
        for symbol in ("BTCUSDT", "ETHUSDT")
    }


def book(symbol, price):
    return OrderBook(
        "linear",
        symbol,
        bids=((price - D("0.01"), D("1000")),),
        asks=((price + D("0.01"), D("1000")),),
        source_ts=now_utc(),
        received_at=now_utc(),
    )


class PublicAdapter:
    def __init__(self):
        self.prices = {"BTCUSDT": D("100"), "ETHUSDT": D("100")}

    async def get_ticker(self, category, symbol):
        price = self.prices[symbol]
        stamp = now_utc()
        return Ticker(
            "linear",
            symbol,
            bid_price=price - D("0.01"),
            ask_price=price + D("0.01"),
            source_ts=stamp,
            received_at=stamp,
        )

    async def get_orderbook(self, category, symbol, limit=50):
        return book(symbol, self.prices[symbol])


def engine(database, adapter):
    return ShadowEngine(adapter=adapter, sessionmaker=database.sessionmaker, settings=config())


async def rows(database):
    async with database.sessionmaker() as session:
        return list(await session.scalars(select(ShadowTrade).order_by(ShadowTrade.id)))


async def test_shadow_samples_and_simulates_without_trade_writes(database):
    adapter = PublicAdapter()
    shadow = engine(database, adapter)
    assert shadow.environment == ENVIRONMENT
    pair = config().pairs[0]
    result = None
    for index, price in enumerate(("100", "100", "100", "100", "103")):
        adapter.prices["BTCUSDT"] = D(price)
        result = await shadow.evaluate_pair(
            pair,
            quotes={},
            specs=specs(),
            quote_status=dict.fromkeys(adapter.prices, True),
            books={symbol: book(symbol, adapter.prices[symbol]) for symbol in adapter.prices},
            now_ms=1_700_000_000_000 + index * 2000,
        )
    assert result is not None
    assert result.decision.action == "OPEN"
    assert result.depth_ok is True
    saved = await rows(database)
    assert len(saved) == 1
    assert saved[-1].environment == "shadow"
    assert saved[-1].qty_a > 0 and saved[-1].qty_b > 0
    assert saved[-1].context["trade_writes"] is False


async def test_shadow_rejects_adapter_with_trade_writes(database):
    class UnsafeAdapter(PublicAdapter):
        allow_trade_writes = True

    with pytest.raises(ValueError, match="allow_trade_writes"):
        ShadowEngine(adapter=UnsafeAdapter(), sessionmaker=database.sessionmaker, settings=config())


async def test_shadow_accepts_public_rest_adapter(database):
    """公开只读 REST 适配器（allow_trade_writes=False）必须被接受。"""

    class PublicRestAdapter(PublicAdapter):
        allow_trade_writes = False

    ShadowEngine(adapter=PublicRestAdapter(), sessionmaker=database.sessionmaker, settings=config())


async def test_shadow_status_is_readonly_and_reports_metrics(database):
    shadow = engine(database, PublicAdapter())
    status = await shadow.status()
    assert status["enabled"] is True
    assert status["observation_days"] == 7
    assert status["promotion_candidate"] is False
    assert "不自动改参数" in status["promotion_note"]
    assert status["review_gate"] == {
        "min_closed_trades": 5,
        "min_sharpe": "2",
        "max_drawdown": "0.03",
    }
    assert status["pair_candidates"] == []
    assert len(status["pair_reviews"]) == 1
    assert status["pair_reviews"][0]["pair_key"] == "BTCUSDT:ETHUSDT"
    assert status["pair_reviews"][0]["closed_trades"] == 0
    assert status["pair_reviews"][0]["promotion_candidate"] is False
    assert await rows(database) == []


async def test_shadow_pair_review_requires_gate_and_stays_readonly(database):
    shadow = engine(database, PublicAdapter())
    now_ms = int(now_utc().timestamp() * 1000)
    pair = config().pairs[0]
    async with database.sessionmaker() as session:
        for index in range(5):
            session.add(
                ShadowTrade(
                    environment=ENVIRONMENT,
                    pair_trade_id=f"shadow-review-{index}",
                    direction="SHORT_A_LONG_B",
                    side_a="Sell",
                    side_b="Buy",
                    pair_key=pair.pair_key,
                    symbol_a=pair.symbol_a,
                    symbol_b=pair.symbol_b,
                    decision_at_ms=now_ms - (5 - index) * 1000,
                    action="CLOSE",
                    reason="EXIT_THRESHOLD_REACHED",
                    qty_a=D("1"),
                    qty_b=D("1"),
                    price_a=D("100"),
                    price_b=D("100"),
                    notional=D("100"),
                    net_pnl=D("0.1"),
                    cumulative_pnl=D("0.1") * (index + 1),
                    context={"simulated": True},
                )
            )
        await session.commit()
    before = [(row.pair_trade_id, row.net_pnl) for row in await rows(database)]
    status = await shadow.status()
    assert status["closed_trades"] == 5
    review = status["pair_reviews"][0]
    assert review["closed_trades"] == 5
    assert D(review["cumulative_pnl"]) == D("0.5")
    # 5 笔但零方差，按口径 Sharpe=0，不能仅凭笔数标成候选。
    assert review["promotion_candidate"] is False
    assert status["pair_candidates"] == []
    assert [(row.pair_trade_id, row.net_pnl) for row in await rows(database)] == before


async def test_shadow_candidate_decides_per_pair_not_on_mixed_portfolio(database):
    """候选必须按单个配对判定；组合口径的亏损不能否掉达标配对，反之亦然。"""
    now_ms = int(now_utc().timestamp() * 1000)
    settings = config().model_copy(
        update={
            "pairs": (
                *config().pairs,
                SpreadSettings.model_validate(
                    {
                        "enabled": False,
                        "pairs": [
                            {
                                "pair_key": "DOGEUSDT:LINKUSDT",
                                "symbol_a": "DOGEUSDT",
                                "symbol_b": "LINKUSDT",
                                "hedge_ratio": "1",
                                "z_entry": "1",
                                "z_exit": "0.2",
                                "z_stop": "3",
                                "max_notional": {"value": "300", "currency": "USDT"},
                                "max_unhedged_notional": {"value": "20", "currency": "USDT"},
                                "enabled": False,
                            }
                        ],
                    }
                ).pairs[0],
            )
        }
    )
    shadow = ShadowEngine(
        adapter=PublicAdapter(), sessionmaker=database.sessionmaker, settings=settings
    )
    good = [D("0.12"), D("0.15"), D("0.10"), D("0.18"), D("0.11")]
    bad = [D("-0.30"), D("-0.20"), D("-0.10"), D("-0.05"), D("-0.02")]
    async with database.sessionmaker() as session:
        for index, pnl in enumerate(good):
            session.add(
                ShadowTrade(
                    environment=ENVIRONMENT,
                    pair_trade_id=f"shadow-good-{index}",
                    direction="SHORT_A_LONG_B",
                    side_a="Sell",
                    side_b="Buy",
                    pair_key="BTCUSDT:ETHUSDT",
                    symbol_a="BTCUSDT",
                    symbol_b="ETHUSDT",
                    decision_at_ms=now_ms - (10 - index) * 1000,
                    action="CLOSE",
                    reason="EXIT_THRESHOLD_REACHED",
                    qty_a=D("1"),
                    qty_b=D("1"),
                    price_a=D("100"),
                    price_b=D("100"),
                    notional=D("100"),
                    net_pnl=pnl,
                    cumulative_pnl=sum(good[: index + 1]),
                    context={"simulated": True},
                )
            )
        for index, pnl in enumerate(bad):
            session.add(
                ShadowTrade(
                    environment=ENVIRONMENT,
                    pair_trade_id=f"shadow-bad-{index}",
                    direction="SHORT_A_LONG_B",
                    side_a="Sell",
                    side_b="Buy",
                    pair_key="DOGEUSDT:LINKUSDT",
                    symbol_a="DOGEUSDT",
                    symbol_b="LINKUSDT",
                    decision_at_ms=now_ms - (10 - index) * 1000,
                    action="CLOSE",
                    reason="EXIT_THRESHOLD_REACHED",
                    qty_a=D("1"),
                    qty_b=D("1"),
                    price_a=D("100"),
                    price_b=D("100"),
                    notional=D("100"),
                    net_pnl=pnl,
                    cumulative_pnl=sum(bad[: index + 1]),
                    context={"simulated": True},
                )
            )
        await session.commit()
    status = await shadow.status()
    reviews = {item["pair_key"]: item for item in status["pair_reviews"]}
    assert reviews["BTCUSDT:ETHUSDT"]["promotion_candidate"] is True
    assert reviews["DOGEUSDT:LINKUSDT"]["promotion_candidate"] is False
    assert status["pair_candidates"] == ["BTCUSDT:ETHUSDT"]
    assert status["promotion_candidate"] is True
    # 组合口径本身是亏损的：它只是参考，不能推翻按配对的判定。
    assert D(status["cumulative_pnl"]) < 0


async def test_shadow_tick_samples_disabled_pairs(database):
    """影子盘对声明的配对全部采样；配对开关只控制真实执行面。"""
    import bybitbot.service as service_module
    from bybitbot.service import TradingService

    base = load_config(CONFIG_PATH)
    shadow_cfg = SpreadSettings.model_validate(
        {
            "enabled": False,
            "pairs": [
                {
                    "pair_key": "BTCUSDT:ETHUSDT",
                    "symbol_a": "BTCUSDT",
                    "symbol_b": "ETHUSDT",
                    "hedge_ratio": "1",
                    "z_entry": "2",
                    "z_exit": "0.5",
                    "z_stop": "3.5",
                    "max_notional": {"value": "100", "currency": "USDT"},
                    "max_unhedged_notional": {"value": "20", "currency": "USDT"},
                    "enabled": False,
                }
            ],
            "shadow": {"enabled": True, "min_depth_notional": "10"},
        }
    )
    cfg = base.model_copy(update={"spread": shadow_cfg})
    service = TradingService(cfg, database, market_adapter=PublicAdapter())
    sampled = []

    class RecordingEngine(ShadowEngine):
        async def evaluate_pair(self, config, **kwargs):
            sampled.append(config.pair_key)
            return

    original = service_module.ShadowEngine
    service_module.ShadowEngine = RecordingEngine
    try:
        await service._shadow_tick()
    finally:
        service_module.ShadowEngine = original
    assert sampled == ["BTCUSDT:ETHUSDT"], "影子盘必须采样禁用配对"


async def test_shadow_samples_half_tick_mids(database):
    """真实盘口中间价经常落在半档（bid=100.0/ask=100.1 → mid=100.05）。

    中间价是推导值而非订单价格，tick 对齐检查会把它全部拒掉，表现为
    「影子盘开启但样本永远为 0」；回归确保半档中间价正常落库。
    """
    adapter = PublicAdapter()
    adapter.prices = {"BTCUSDT": D("100"), "ETHUSDT": D("100")}
    shadow = engine(database, adapter)
    pair = config().pairs[0]
    # tick_size=0.01、bid=99.99/ask=100.01 → mid=100.00 恰好整档；
    # 改成 bid=100.00/ask=100.01 → mid=100.005 是半档，修复前会被拒。
    half_tick_ticker = Ticker(
        "linear",
        "BTCUSDT",
        bid_price=D("100.00"),
        ask_price=D("100.01"),
        source_ts=now_utc(),
        received_at=now_utc(),
    )
    sampled = []
    original_record = shadow._record_sample

    async def recording(config, **kwargs):
        sampled.append(kwargs["ticker_a"])
        return await original_record(config, **kwargs)

    shadow._record_sample = recording
    await shadow.evaluate_pair(
        pair,
        quotes={"BTCUSDT": half_tick_ticker, "ETHUSDT": None},
        specs=specs(),
        quote_status={"BTCUSDT": True, "ETHUSDT": True},
        books={},
        now_ms=1_700_000_000_000,
    )
    assert sampled, "半档中间价不应触发 SignalUnavailable"
    assert shadow._mid(sampled[0]) == D("100.005")


async def test_shadow_warns_throttled_when_sampling_rejected(database, monkeypatch):
    """采样被拒必须留痕（限频），静默吞掉会掩盖「永远 0 条样本」的故障。"""
    from bybitbot.spread import shadow as shadow_module

    shadow_module._WARN_AT.clear()
    warnings: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        shadow_module, "logger", logging.getLogger("shadow-test-probe"), raising=True
    )
    probe = logging.getLogger("shadow-test-probe")
    monkeypatch.setattr(
        shadow_module.logger,
        "warning",
        lambda msg, *args: warnings.append((msg % args) if args else msg),
        raising=True,
    )
    probe.setLevel(logging.WARNING)
    try:
        shadow = engine(database, PublicAdapter())
        pair = config().pairs[0]
        stale_specs = dict(specs())
        stale_specs["ETHUSDT"] = None
        for _ in range(3):
            await shadow.evaluate_pair(
                pair,
                quotes={},
                specs=stale_specs,
                quote_status={"BTCUSDT": True, "ETHUSDT": True},
                books={},
                now_ms=1_700_000_000_000,
            )
        assert len(warnings) == 1, "同一配对 10 分钟内只允许一条拒绝日志"
        assert "影子盘采样被拒" in warnings[0]
        assert "BTCUSDT:ETHUSDT" in warnings[0]
    finally:
        shadow_module._WARN_AT.clear()


async def test_shadow_tick_refreshes_stale_cache_legs(database):
    """缓存行情过期时影子盘用公共只读适配器自行刷新，而不是拒绝采样。

    worker tick 后段（账户同步、验证采样等）可能耗时数十秒；等 _shadow_tick
    跑到时 _market() 拉的行情已超过 max_quote_age_seconds，此前表现为
    「缓存有数据但永远判定过期、样本为 0」。
    """
    from bybitbot.service import TradingService

    base = load_config(CONFIG_PATH)
    shadow_cfg = SpreadSettings.model_validate(
        {
            "enabled": False,
            "pairs": [
                {
                    "pair_key": "BTCUSDT:ETHUSDT",
                    "symbol_a": "BTCUSDT",
                    "symbol_b": "ETHUSDT",
                    "hedge_ratio": "1",
                    "z_entry": "2",
                    "z_exit": "0.5",
                    "z_stop": "3.5",
                    "max_notional": {"value": "100", "currency": "USDT"},
                    "max_unhedged_notional": {"value": "20", "currency": "USDT"},
                    "enabled": False,
                }
            ],
            "auto": {
                "enabled": True,
                "window": 5,
                "sample_interval_seconds": 2,
                "fee_rate": "0",
                "slippage_bps": "0",
                "target_notional": "100",
            },
            "shadow": {"enabled": True, "min_depth_notional": "10"},
        }
    )
    cfg = base.model_copy(update={"spread": shadow_cfg})
    adapter = PublicAdapter()
    service = TradingService(cfg, database, market_adapter=adapter)
    # 预填过期缓存：时间戳远超 max_quote_age_seconds（默认 15 秒）。
    stale_stamp = now_utc() - timedelta(seconds=120)
    for symbol in ("BTCUSDT", "ETHUSDT"):
        service.quotes.put_ticker(
            Ticker(
                "linear",
                symbol,
                bid_price=D("1"),
                ask_price=D("1.01"),
                source_ts=stale_stamp,
                received_at=stale_stamp,
            )
        )
        service.quotes.put_book(
            OrderBook(
                "linear",
                symbol,
                bids=((D("1"), D("10")),),
                asks=((D("1.01"), D("10")),),
                source_ts=stale_stamp,
                received_at=stale_stamp,
            )
        )
        service.specs[("linear", symbol)] = specs()[symbol]
    refreshed_symbols: list[str] = []
    original_get_ticker = adapter.get_ticker

    async def recording_get_ticker(category, symbol):
        refreshed_symbols.append(symbol)
        return await original_get_ticker(category, symbol)

    adapter.prices["BTCUSDT"] = D("101")  # 刷新后 mid=101.00，与历史样本可区分
    adapter.get_ticker = recording_get_ticker
    # 预填 4 条历史样本（间隔 2 秒、各占独立 at_ms 桶）：样本窗口=5，
    # 影子盘刷新后记录第 5 条即可通过信号判定的样本量门槛。
    now_ms = int(now_utc().timestamp() * 1000)
    async with database.sessionmaker() as session:
        for offset in range(4):
            session.add(
                SpreadSignalSample(
                    environment=ENVIRONMENT,
                    pair_key="BTCUSDT:ETHUSDT",
                    at_ms=now_ms - (8 - offset * 2) * 1000,
                    price_a=D("100") + D(offset) * D("0.01"),
                    price_b=D("100"),
                    funding_a=D("0"),
                    funding_b=D("0"),
                    raw={"source": "test_baseline"},
                )
            )
        await session.commit()
    await service._shadow_tick()
    assert set(refreshed_symbols) == {"BTCUSDT", "ETHUSDT"}, "过期腿必须刷新"
    async with database.sessionmaker() as session:
        latest = await session.scalar(
            select(SpreadSignalSample)
            .where(
                SpreadSignalSample.environment == ENVIRONMENT,
                SpreadSignalSample.pair_key == "BTCUSDT:ETHUSDT",
            )
            .order_by(SpreadSignalSample.id.desc())
            .limit(1)
        )
    assert latest is not None, "刷新后影子盘样本必须落库，而不是永远 0 条"
    assert latest.price_a == D("101.00"), "落库样本必须来自刷新后的新行情"
