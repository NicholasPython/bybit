"""阶段三自动信号的持久化窗口、冷却与 worker 接入测试（隔离 PostgreSQL）。"""

from __future__ import annotations

from decimal import Decimal as D

from sqlalchemy import select

from bybitbot.adapters.base import InstrumentSpec, Ticker
from bybitbot.clock import now_utc
from bybitbot.db.models import RuntimeState
from bybitbot.market import MarketStatus
from bybitbot.service import TradingService
from bybitbot.settings.loader import load_config
from bybitbot.settings.models import SpreadSettings
from bybitbot.spread.auto import DECISION_KEY, STOP_KEY, SpreadAutoSignal
from bybitbot.spread.models import SpreadPairTrade, SpreadSignalSample
from bybitbot.spread.paper import LinearPaperAdapter
from bybitbot.spread.signals import ACTION_OPEN, ACTION_STOP


def settings(*, window=5, interval=2, cooldown=60, stop_z="1.8", entry_z="1"):
    return SpreadSettings.model_validate(
        {
            "enabled": True,
            "pairs": [
                {
                    "pair_key": "BTCUSDT:ETHUSDT",
                    "symbol_a": "BTCUSDT",
                    "symbol_b": "ETHUSDT",
                    "hedge_ratio": "1",
                    "z_entry": entry_z,
                    "z_exit": "0.2",
                    "z_stop": stop_z,
                    "max_notional": {"value": "300", "currency": "USDT"},
                    "max_unhedged_notional": {"value": "20", "currency": "USDT"},
                    "recovery_window_ms": 500,
                    "enabled": True,
                }
            ],
            "auto": {
                "enabled": True,
                "window": window,
                "sample_interval_seconds": interval,
                "fee_rate": "0",
                "slippage_bps": "0",
                "funding_buffer": "0",
                "target_notional": "100",
                "stop_cooldown_seconds": cooldown,
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


def status():
    return {symbol: MarketStatus(True) for symbol in ("BTCUSDT", "ETHUSDT")}


def ticker(symbol, price, *, now=None):
    stamp = now or now_utc()
    return Ticker(
        "linear",
        symbol,
        bid_price=D(price) - D("0.01"),
        ask_price=D(price) + D("0.01"),
        source_ts=stamp,
        received_at=stamp,
    )


class FakeAdapter:
    def __init__(self):
        self.prices = {"BTCUSDT": D("100"), "ETHUSDT": D("100")}

    async def get_ticker(self, category, symbol):
        return ticker(symbol, self.prices[symbol])


async def engine(database, *, config=None, adapter=None, uid="paper-account"):
    return SpreadAutoSignal(
        adapter=adapter or FakeAdapter(),
        sessionmaker=database.sessionmaker,
        environment="paper",
        account_uid=uid,
        settings=config or settings(),
    )


async def rows(database, model):
    async with database.sessionmaker() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


async def test_window_persists_and_builds_open_signal(database):
    adapter = FakeAdapter()
    signal_engine = await engine(database, adapter=adapter, config=settings(stop_z="2.5"))
    config = settings(stop_z="2.5").pairs[0]
    result = None
    prices = ("100", "100", "100", "100")
    for index, price in enumerate(prices):
        adapter.prices["BTCUSDT"] = D(price)
        result = await signal_engine.evaluate_pair(
            config,
            quotes={},
            specs=specs(),
            quote_status=status(),
            now_ms=2000 * (index + 1),
        )
    assert result.decision is None  # 前三轮窗口不足，最后一轮标准差退化。

    adapter.prices["BTCUSDT"] = D("101")
    result = await signal_engine.evaluate_pair(
        config,
        quotes={},
        specs=specs(),
        quote_status=status(),
        now_ms=2000 * (len(prices) + 1),
    )
    assert result.decision.action == ACTION_OPEN
    assert result.signal is not None
    assert result.signal.side_a == "Sell"
    assert result.signal.side_b == "Buy"
    assert len(await rows(database, SpreadSignalSample)) == 5
    decision_row = (await rows(database, RuntimeState))[0]
    assert decision_row.key == DECISION_KEY + config.pair_key
    assert decision_row.value["action"] == ACTION_OPEN


async def test_stop_sets_persistent_cooldown_and_closes_active_pair(database):
    adapter = FakeAdapter()
    signal_engine = await engine(database, adapter=adapter)
    config = settings().pairs[0]
    for index in range(4):
        adapter.prices["BTCUSDT"] = D("100")
        await signal_engine.evaluate_pair(
            config,
            quotes={},
            specs=specs(),
            quote_status=status(),
            now_ms=2000 * (index + 1),
        )
    adapter.prices["BTCUSDT"] = D("103")
    result = await signal_engine.evaluate_pair(
        config,
        quotes={},
        specs=specs(),
        quote_status=status(),
        now_ms=10000,
    )
    assert result.decision.action == ACTION_STOP
    assert result.signal is None
    cooldown = [row for row in await rows(database, RuntimeState) if row.key.startswith(STOP_KEY)]
    assert len(cooldown) == 1
    assert cooldown[0].value["until_ms"] == 10000 + 60 * 1000
    restarted = await engine(database, adapter=adapter)
    blocked = await restarted.evaluate_pair(
        config,
        quotes={},
        specs=specs(),
        quote_status=status(),
        now_ms=11000,
    )
    assert blocked.decision.action == ACTION_STOP
    assert blocked.decision.action == ACTION_STOP
    assert blocked.signal is None


async def test_invalid_quote_does_not_pollute_window(database):
    signal_engine = await engine(database)
    config = settings().pairs[0]
    stale = status()
    stale["BTCUSDT"] = MarketStatus(False, "MARKET_DATA_STALE")
    for now_ms in (1000, 2000, 3000, 4000, 5000):
        result = await signal_engine.evaluate_pair(
            config,
            quotes={},
            specs=specs(),
            quote_status=stale,
            now_ms=now_ms,
        )
        assert result.decision is None
    assert await rows(database, SpreadSignalSample) == []


async def test_half_tick_mid_is_accepted(database):
    """真实盘口中间价经常落在半档（tick=0.01 时 bid=100.0/ask=100.01 → 100.005）。

    中间价是推导值而非订单价格，tick 对齐检查会把它全部拒掉，表现为
    「信号开启但样本永远为 0」；回归确保半档中间价正常落库。
    """
    from bybitbot.spread import auto as auto_module

    auto_module._WARN_AT.clear()
    try:
        signal_engine = await engine(database)
        config = settings().pairs[0]
        half_tick = Ticker(
            "linear",
            "BTCUSDT",
            bid_price=D("100.00"),
            ask_price=D("100.01"),
            source_ts=now_utc(),
            received_at=now_utc(),
        )
        quotes = {"BTCUSDT": half_tick, "ETHUSDT": ticker("ETHUSDT", 100)}
        for index in range(5):
            await signal_engine.evaluate_pair(
                config,
                quotes=quotes,
                specs=specs(),
                quote_status=status(),
                now_ms=1000 * (index + 1),
            )
        saved = await rows(database, SpreadSignalSample)
        assert saved, "半档中间价不应触发 SignalUnavailable"
        assert {row.price_a for row in saved} == {D("100.005")}
    finally:
        auto_module._WARN_AT.clear()


async def test_entry_does_not_fire_without_authority(database, monkeypatch):
    config = settings()
    config = config.model_copy(
        update={"pairs": tuple(pair.model_copy(update={"enabled": True}) for pair in config.pairs)}
    )
    adapter = LinearPaperAdapter(initial_balances={"USDT": D("1000")})
    for symbol in ("BTCUSDT", "ETHUSDT"):
        adapter.set_instrument(specs()[symbol])
    config = load_config("config/config.example.yaml").model_copy(update={"spread": config})
    service = TradingService(config, database, market_adapter=adapter)
    engine = SpreadAutoSignal(
        adapter=FakeAdapter(),
        sessionmaker=database.sessionmaker,
        environment="paper",
        account_uid=service.uid,
        settings=config.spread,
    )
    monkeypatch.setattr(
        "bybitbot.service.SpreadAutoSignal",
        lambda **kwargs: engine,
    )
    await service._spread_mode_check()
    await service._apply_config()
    try:
        for index in range(5):
            await engine.evaluate_pair(
                config.spread.pairs[0],
                quotes={},
                specs=specs(),
                quote_status=status(),
                now_ms=(index + 1) * 60000,
            )
        adapter.prices = {"BTCUSDT": D("103"), "ETHUSDT": D("100")}
        await service._spread_auto_tick()
        assert await rows(database, SpreadPairTrade) == []
    finally:
        await service.close()
