"""FR-07.3 影子盘：公开行情、深度模拟、无真实订单与只读投影测试。"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from sqlalchemy import select

from bybitbot.adapters.base import InstrumentSpec, OrderBook, Ticker
from bybitbot.clock import now_utc
from bybitbot.settings.models import SpreadSettings
from bybitbot.spread.models import ShadowTrade
from bybitbot.spread.shadow import ENVIRONMENT, ShadowEngine


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
    assert await rows(database) == []
