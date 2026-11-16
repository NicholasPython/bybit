"""离线合成行情；只用于 paper 系统演练，不表示真实价格或交易收益。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from bybitbot.adapters.base import InstrumentSpec, OrderBook, Ticker
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import now_utc
from bybitbot.terms import option_symbol


class SyntheticMarket(PaperAdapter):
    """固定价格和可见深度，每次读取提供新时间戳；合约到期日由配置固定。

    价格不随重启漂移。期权过期后不滚动合约、不猜测交割价，仍由正常风控
    拒绝过期合约；长期演练应创建新的隔离沙盒。
    """

    def __init__(self, expiry: dt.datetime):
        super().__init__()
        self.prices: dict[tuple[str, str], tuple[D, D, D]] = {}
        for base, index, premium in (("BTC", D("100000"), D("1000")), ("ETH", D("3000"), D("100"))):
            spot = f"{base}USDT"
            self.set_instrument(
                InstrumentSpec(
                    "spot",
                    spot,
                    base,
                    "USDT",
                    "Trading",
                    tick_size=D("0.1"),
                    qty_step=D("0.000001"),
                    min_order_qty=D("0.000001"),
                    min_order_amt=D("1"),
                    max_order_qty=D("100"),
                )
            )
            self.prices[("spot", spot)] = (index - D("0.1"), index, index)
            symbol = option_symbol(base=base, expiry=expiry, strike=index, option_type="Call")
            self.set_instrument(
                InstrumentSpec(
                    "option",
                    symbol,
                    base,
                    "USDT",
                    "Trading",
                    tick_size=D("0.1"),
                    qty_step=D("0.01"),
                    min_order_qty=D("0.01"),
                    max_order_qty=D("100"),
                    settle_coin="USDT",
                    options_type="Call",
                    strike_price=index,
                    delivery_time=expiry,
                )
            )
            self.prices[("option", symbol)] = (premium, premium + D("0.1"), index)

    async def get_ticker(self, category, symbol):
        bid, ask, index = self.prices[(category, symbol)]
        moment = now_utc()
        option = category == "option"
        return Ticker(
            category,
            symbol,
            bid_price=bid,
            ask_price=ask,
            bid_size=D("10"),
            ask_size=D("10"),
            mark_price=(bid + ask) / 2 if option else None,
            index_price=index if option else None,
            delta=D("0.45") if option else None,
            iv=D("0.6") if option else None,
            open_interest=D("100") if option else None,
            source_ts=moment,
            received_at=moment,
        )

    async def get_tickers(self, category, *, base_coin=None):
        return [
            await self.get_ticker(category, spec.symbol)
            for spec in await self.get_instruments(category)
            if base_coin is None or spec.base_coin == base_coin
        ]

    async def get_orderbook(self, category, symbol, limit=50):
        bid, ask, _ = self.prices[(category, symbol)]
        moment = now_utc()
        return OrderBook(
            category,
            symbol,
            ((bid, D("10")),),
            ((ask, D("10")),),
            source_ts=moment,
            received_at=moment,
        )
