"""合约目录与行情同步（MKT-01 / MKT-02 / MKT-03）。

需求原文：
    **MKT-01** 动态获取并缓存交易规格：产品类别、symbol、标的、报价币、结算币、
    价格步长、数量步长、最小订单限制；期权另保存方向、行权价、到期时间及状态。
    处理分页、更新和退市。只允许白名单内、当前可交易的品种开仓。
    字段缺失或解析失败时拒绝相关交易。

    **MKT-02** 保存行情来源时间、接收时间与数据有效性。使用买卖报价和所需深度
    判断价差及可执行价格；标记价仅用于指定估值用途。盘口快照/增量出现断档时重建，
    重建前不得使用不完整数据批准订单。数据过期或交叉报价等异常时暂停相关开仓。

    **MKT-03** 订阅重连后重新验证行情及账户快照，不能把「连接成功」视为
    「数据已恢复」。期权开仓所需的 IV、Delta 等缺失、过期或单位未经验证时，
    相关规则不执行。记录策略决策使用的数据快照引用。

关键设计
--------
    * 字段缺失 → ``InstrumentSpec`` 的对应字段为 ``None`` → 上层**拒绝**该品种
      的交易，而不是用默认值蒙混过关。
    * 行情快照带 ``anomalous`` 标记；交叉报价、过期、断档都会置位。
    * 每次策略决策引用一个 ``quote_snapshot_id``，保证可追溯（STR-01）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from bybitbot.adapters.base import InstrumentSpec, OrderBook, Ticker, TradingAdapter
from bybitbot.clock import now_utc
from bybitbot.db.models import Instrument, QuoteSnapshot


@dataclass
class MarketStatus:
    """某个品种的行情可用性。"""

    usable: bool
    reason_code: str = ""
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return not self.usable


class InstrumentCatalog:
    """合约目录缓存（MKT-01）。

    同步时把交易所规格写入 ``instrument`` 表；退市的品种保留记录但
    ``status`` 不再是 ``Trading``，因此不会被开仓。
    """

    def __init__(self, sessionmaker, environment: str) -> None:
        self.sessionmaker = sessionmaker
        self.environment = environment

    async def sync(
        self, adapter: TradingAdapter, categories: tuple[str, ...] = ("spot", "option")
    ) -> int:
        """同步合约目录。返回写入的品种数。"""
        count = 0
        for category in categories:
            specs = await adapter.get_instruments(category)
            seen = [spec.symbol for spec in specs]
            async with self.sessionmaker() as session:
                await session.execute(
                    update(Instrument)
                    .where(
                        Instrument.environment == self.environment,
                        Instrument.category == category,
                        Instrument.symbol.notin_(seen),
                    )
                    .values(status="MissingFromCatalog", synced_at=now_utc())
                )
                for spec in specs:
                    values = {
                        "environment": self.environment,
                        "category": spec.category,
                        "symbol": spec.symbol,
                        "base_coin": spec.base_coin,
                        "quote_coin": spec.quote_coin,
                        "settle_coin": spec.settle_coin,
                        "status": spec.status,
                        "tick_size": spec.tick_size,
                        "qty_step": spec.qty_step,
                        "min_order_qty": spec.min_order_qty,
                        "max_order_qty": spec.max_order_qty,
                        "min_order_amt": spec.min_order_amt,
                        "max_order_amt": spec.max_order_amt,
                        "options_type": spec.options_type,
                        "strike_price": spec.strike_price,
                        "delivery_time": spec.delivery_time,
                        "raw": spec.raw,
                        "synced_at": now_utc(),
                    }
                    statement = insert(Instrument).values(**values)
                    await session.execute(
                        statement.on_conflict_do_update(
                            constraint="uq_instrument",
                            set_=values,
                        )
                    )
                    count += 1
                await session.commit()
        return count

    async def _upsert(self, spec: InstrumentSpec) -> None:
        async with self.sessionmaker() as session:
            row = (
                await session.execute(
                    select(Instrument).where(
                        Instrument.environment == self.environment,
                        Instrument.category == spec.category,
                        Instrument.symbol == spec.symbol,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = Instrument(
                    environment=self.environment,
                    category=spec.category,
                    symbol=spec.symbol,
                )
                session.add(row)
            row.base_coin = spec.base_coin
            row.quote_coin = spec.quote_coin
            row.settle_coin = spec.settle_coin
            row.status = spec.status
            row.tick_size = spec.tick_size
            row.qty_step = spec.qty_step
            row.min_order_qty = spec.min_order_qty
            row.max_order_qty = spec.max_order_qty
            row.min_order_amt = spec.min_order_amt
            row.max_order_amt = spec.max_order_amt
            row.options_type = spec.options_type
            row.strike_price = spec.strike_price
            row.delivery_time = spec.delivery_time
            row.raw = spec.raw
            row.synced_at = now_utc()
            await session.commit()

    async def get(self, category: str, symbol: str) -> InstrumentSpec | None:
        async with self.sessionmaker() as session:
            row = (
                await session.execute(
                    select(Instrument).where(
                        Instrument.environment == self.environment,
                        Instrument.category == category,
                        Instrument.symbol == symbol,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _spec_of(row)

    async def list(self, category: str) -> list[InstrumentSpec]:
        async with self.sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(Instrument).where(
                        Instrument.environment == self.environment,
                        Instrument.category == category,
                    )
                )
            )
        return [_spec_of(r) for r in rows]


def _spec_of(row: Instrument) -> InstrumentSpec:
    return InstrumentSpec(
        category=row.category,  # type: ignore[arg-type]
        symbol=row.symbol,
        base_coin=row.base_coin,
        quote_coin=row.quote_coin,
        status=row.status,
        tick_size=row.tick_size,
        qty_step=row.qty_step,
        min_order_qty=row.min_order_qty,
        max_order_qty=row.max_order_qty,
        min_order_amt=row.min_order_amt,
        max_order_amt=row.max_order_amt,
        settle_coin=row.settle_coin,
        options_type=row.options_type,  # type: ignore[arg-type]
        strike_price=row.strike_price,
        delivery_time=row.delivery_time,
        raw=row.raw or {},
    )


class QuoteCache:
    """行情缓存与有效性判定（MKT-02）。

    缓存是**进程内**的：重启后没有行情就必须重新同步，而不是用旧值
    （MKT-03：不能把「连接成功」视为「数据已恢复」）。
    """

    def __init__(self, *, max_age_seconds: int = 15, environment: str = "paper") -> None:
        self.max_age_seconds = max_age_seconds
        self.environment = environment
        self._quotes: dict[tuple[str, str], Ticker] = {}
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._snapshots: dict[tuple[str, str], int] = {}

    def put_ticker(self, ticker: Ticker) -> None:
        self._quotes[(ticker.category, ticker.symbol)] = ticker

    def put_book(self, book: OrderBook) -> None:
        self._books[(book.category, book.symbol)] = book

    def get_ticker(self, category: str, symbol: str) -> Ticker | None:
        return self._quotes.get((category, symbol))

    def get_book(self, category: str, symbol: str) -> OrderBook | None:
        return self._books.get((category, symbol))

    def status(
        self,
        category: str,
        symbol: str,
        *,
        now: dt.datetime | None = None,
        require_greeks: bool = False,
    ) -> MarketStatus:
        """判断某个品种的行情是否可用于批准订单。"""
        moment = now or now_utc()
        ticker = self._quotes.get((category, symbol))
        book = self._books.get((category, symbol))

        if ticker is None and book is None:
            return MarketStatus(False, "NO_MARKET_DATA", f"{symbol} 没有任何行情数据")
        if book is not None and book.gap_detected:
            return MarketStatus(False, "MARKET_DATA_GAP", f"{symbol} 盘口出现断档，重建前不得使用")

        for reference in (ticker, book):
            if reference is None:
                continue
            for stamp in (reference.source_ts, reference.received_at):
                if stamp is None:
                    return MarketStatus(False, "MARKET_DATA_NO_TIMESTAMP", f"{symbol} 缺少时间戳")
                age = (moment - stamp).total_seconds()
                if age < -5 or age > self.max_age_seconds:
                    return MarketStatus(
                        False, "MARKET_DATA_STALE", f"{symbol} 行情时间无效 {age:.1f}s"
                    )
        if book is not None:
            if not book.bids or not book.asks or book.bid() >= book.ask():
                return MarketStatus(False, "INVALID_BOOK", f"{symbol} 缺少有效双侧深度")
            if any(price <= 0 or qty < 0 for price, qty in (*book.bids, *book.asks)):
                return MarketStatus(False, "INVALID_BOOK", f"{symbol} 深度包含无效价格或数量")
        if ticker is not None and (
            ticker.bid_price is None
            or ticker.ask_price is None
            or ticker.bid_price <= 0
            or ticker.ask_price <= 0
        ):
            return MarketStatus(False, "INVALID_QUOTE", f"{symbol} 缺少有效报价")

        if ticker is not None and ticker.is_crossed:
            return MarketStatus(False, "CROSSED_QUOTE", f"{symbol} 交叉报价（买价高于卖价）")

        if category == "option":
            if ticker is None:
                return MarketStatus(False, "NO_OPTION_TICKER", f"{symbol} 缺少期权行情")
            if ticker.mark_price is None:
                return MarketStatus(False, "NO_MARK_PRICE", f"{symbol} 缺少标记价，无法估值")
            if require_greeks:
                if ticker.delta is None:
                    return MarketStatus(False, "NO_DELTA", f"{symbol} 缺少 Delta，规则不执行")
                if ticker.iv is None:
                    return MarketStatus(False, "NO_IV", f"{symbol} 缺少 IV")
        return MarketStatus(True)

    async def persist_snapshot(
        self,
        sessionmaker,
        category: str,
        symbol: str,
        ticker: Ticker,
        *,
        book: OrderBook | None = None,
    ) -> int:
        """把行情写入 ``quote_snapshot``，返回快照 id（供决策引用）。"""
        anomalous = ticker.is_crossed
        if book is not None and book.gap_detected:
            anomalous = True
        async with sessionmaker() as session:
            snapshot = QuoteSnapshot(
                environment=self.environment,
                category=category,
                symbol=symbol,
                source_ts=ticker.source_ts,
                received_at=ticker.received_at or now_utc(),
                bid_price=ticker.bid_price,
                bid_size=ticker.bid_size,
                ask_price=ticker.ask_price,
                ask_size=ticker.ask_size,
                mark_price=ticker.mark_price,
                index_price=ticker.index_price,
                anomalous=anomalous,
                raw={
                    "has_book": book is not None,
                    "delta": str(ticker.delta) if ticker.delta is not None else None,
                    "iv": str(ticker.iv) if ticker.iv is not None else None,
                    "gamma": str(ticker.gamma) if ticker.gamma is not None else None,
                    "vega": str(ticker.vega) if ticker.vega is not None else None,
                    "theta": str(ticker.theta) if ticker.theta is not None else None,
                    "bids": [[str(p), str(q)] for p, q in book.bids] if book else [],
                    "asks": [[str(p), str(q)] for p, q in book.asks] if book else [],
                    "book_source_ts": book.source_ts.isoformat()
                    if book and book.source_ts
                    else None,
                    "book_received_at": book.received_at.isoformat()
                    if book and book.received_at
                    else None,
                    "book_update_id": book.update_id if book else None,
                },
            )
            session.add(snapshot)
            await session.commit()
            await session.refresh(snapshot)
            self._snapshots[(category, symbol)] = snapshot.id
            return snapshot.id

    def snapshot_id(self, category: str, symbol: str) -> int | None:
        return self._snapshots.get((category, symbol))


class MarketView:
    """给策略看的只读视图（实现 :class:`StrategyDeps`）。"""

    def __init__(
        self,
        *,
        catalog: InstrumentCatalog,
        quotes: QuoteCache,
        now_fn=now_utc,
    ) -> None:
        self.catalog = catalog
        self.quotes = quotes
        self._now_fn = now_fn

    @property
    def now(self) -> dt.datetime:
        return self._now_fn()

    async def instrument(self, category: str, symbol: str) -> InstrumentSpec | None:
        return await self.catalog.get(category, symbol)

    async def instruments(self, category: str) -> list[InstrumentSpec]:
        return await self.catalog.list(category)

    def quote(self, category: str, symbol: str) -> Ticker | None:
        return self.quotes.get_ticker(category, symbol)

    def config(self) -> dict[str, object]:
        return {}


def spread_ratio(ticker: Ticker) -> Decimal | None:
    """相对价差（买一/卖一）。无有效报价时返回 ``None``。"""
    if ticker.bid_price is None or ticker.ask_price is None:
        return None
    if ticker.ask_price <= 0:
        return None
    if ticker.bid_price > ticker.ask_price:
        return None
    return (ticker.ask_price - ticker.bid_price) / ticker.ask_price


__all__ = [
    "InstrumentCatalog",
    "MarketStatus",
    "MarketView",
    "QuoteCache",
    "spread_ratio",
]
