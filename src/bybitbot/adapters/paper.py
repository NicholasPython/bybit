"""纸面（Paper）交易适配器（STR-05）。

需求原文：
    Paper 使用可重复的模拟成交模型，报告明确标记模拟。开仓不得因标记价到达就
    自动全额成交；用有效买卖报价、可见深度、配置费用及下文撮合规则计算。
    支持部分成交、取消及结果未知等故障注入；故障实验报告与策略表现报告分别标识。

    Paper 最小模型：以意图接收之后的**下一份有效盘口**为最早成交时点；
    买限价触及卖盘、卖限价触及买盘时，按价格优先遍历限价以内的可见深度。
    相同快照的可用深度在所有模拟订单间共享扣减，处理顺序固定。未触价挂单保留，
    不推测队列位置或未显示流动性；手续费使用明确版本的保守费率。
    模拟余额由独立初始资金配置提供。该模型不刻画真实排队、延迟及市场冲击，
    结果不作为实际可成交性证明。

三条硬约束：
    * 不装载任何交易凭据（ENV-01）；
    * 不发送任何交易写请求到任何交易所；
    * 结果必须在报告里标记为模拟（STR-05）。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from bybitbot.adapters.base import (
    AccountView,
    CancelAck,
    CoinBalance,
    ExecutionView,
    InstrumentSpec,
    KeyInfo,
    OrderAck,
    OrderBook,
    OrderRejected,
    OrderRequest,
    OrderUncertain,
    OrderView,
    PositionView,
    Ticker,
)
from bybitbot.clock import now_utc
from bybitbot.money import dec
from bybitbot.settings.models import OrderSide, OrderType, TimeInForce

ACTIVE_STATUSES = frozenset({"New", "PartiallyFilled"})


class PaperFault(str):
    """故障注入类型。"""


@dataclass(frozen=True)
class FeeSchedule:
    """手续费方案。

    ``version`` 必须显式写明，报告要带上它 —— 手续费版本变了，历史结果不可比。
    """

    version: str
    spot_taker_rate: Decimal
    option_taker_rate: Decimal
    #: 期权按张收费时每张的费用（若有）。
    option_fee_per_contract: Decimal = Decimal("0")

    @classmethod
    def conservative_default(cls) -> FeeSchedule:
        """保守费率：宁可高估成本，不要低估。"""
        return cls(
            version="paper-v1-conservative",
            spot_taker_rate=Decimal("0.001"),
            option_taker_rate=Decimal("0.0015"),
        )


@dataclass
class _DepthPool:
    """一次快照内共享的可见深度。

    相同快照的可用深度在所有模拟订单间共享扣减（STR-05）。
    """

    snapshot_id: int
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]

    def take_asks(self, limit_price: Decimal, wanted: Decimal) -> list[tuple[Decimal, Decimal]]:
        """按价格优先取卖盘深度，直到触及限价或取满。"""
        taken: list[tuple[Decimal, Decimal]] = []
        remaining = wanted
        for idx, (price, size) in enumerate(self.asks):
            if price > limit_price:
                break
            if remaining <= 0:
                break
            available = size - self._consumed_ask(idx)
            if available <= 0:
                continue
            fill = min(available, remaining)
            taken.append((price, fill))
            remaining -= fill
            self._consume_ask(idx, fill)
        return taken

    def take_bids(self, limit_price: Decimal, wanted: Decimal) -> list[tuple[Decimal, Decimal]]:
        """按价格优先取买盘深度。"""
        taken: list[tuple[Decimal, Decimal]] = []
        remaining = wanted
        for idx, (price, size) in enumerate(self.bids):
            if price < limit_price:
                break
            if remaining <= 0:
                break
            available = size - self._consumed_bid(idx)
            if available <= 0:
                continue
            fill = min(available, remaining)
            taken.append((price, fill))
            remaining -= fill
            self._consume_bid(idx, fill)
        return taken

    _ask_consumed: list[Decimal] = field(default_factory=list)
    _bid_consumed: list[Decimal] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self._ask_consumed:
            self._ask_consumed = [Decimal("0")] * len(self.asks)
        if not self._bid_consumed:
            self._bid_consumed = [Decimal("0")] * len(self.bids)

    def _consumed_ask(self, idx: int) -> Decimal:
        return self._ask_consumed[idx]

    def _consume_ask(self, idx: int, amount: Decimal) -> None:
        self._ask_consumed[idx] += amount

    def _consumed_bid(self, idx: int) -> Decimal:
        return self._bid_consumed[idx]

    def _consume_bid(self, idx: int, amount: Decimal) -> None:
        self._bid_consumed[idx] += amount


@dataclass
class PaperOrder:
    """模拟订单。"""

    order_link_id: str
    request: OrderRequest
    created_at: dt.datetime
    qty_filled: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    status: str = "New"
    order_id: str = ""
    executions: list[ExecutionView] = field(default_factory=list)

    @property
    def leaves_qty(self) -> Decimal:
        return self.request.qty - self.qty_filled


@dataclass
class PaperAccount:
    """模拟余额。初始资金由配置提供（STR-05）。"""

    coins: dict[str, CoinBalance] = field(default_factory=dict)

    def balance(self, coin: str) -> CoinBalance:
        return self.coins.get(
            coin.upper(),
            CoinBalance(coin=coin.upper(), equity=Decimal("0"), available=Decimal("0")),
        )

    def credit(self, coin: str, amount: Decimal) -> None:
        current = self.balance(coin)
        self.coins[coin.upper()] = CoinBalance(
            coin=current.coin,
            equity=current.equity + amount,
            available=current.available + amount,
            wallet_balance=(
                current.wallet_balance if current.wallet_balance is not None else current.equity
            )
            + amount,
            usd_value=current.usd_value,
            borrowed=current.borrowed,
            accrued_interest=current.accrued_interest,
            locked=current.locked,
        )

    def debit(self, coin: str, amount: Decimal) -> None:
        current = self.balance(coin)
        self.coins[coin.upper()] = CoinBalance(
            coin=current.coin,
            equity=current.equity - amount,
            available=current.available - amount,
            wallet_balance=(
                current.wallet_balance if current.wallet_balance is not None else current.equity
            )
            - amount,
            usd_value=current.usd_value,
            borrowed=current.borrowed,
            accrued_interest=current.accrued_interest,
            locked=current.locked,
        )


class PaperAdapter:
    """确定性的模拟交易适配器。

    与真实适配器实现同一个 :class:`~bybitbot.adapters.base.TradingAdapter` 接口，
    因此同一套验收用例可以在纸面与联调环境复用。
    """

    environment = "paper"
    allow_trade_writes = False  # ENV-01：paper 不发送交易写请求

    def __init__(
        self,
        *,
        initial_balances: dict[str, Decimal] | None = None,
        fee_schedule: FeeSchedule | None = None,
        account_uid: str = "paper-account",
        account_mode: str = "unified",
        clock: Callable[[], dt.datetime] = now_utc,
    ) -> None:
        self._clock = clock
        self._account = PaperAccount()
        for coin, amount in (initial_balances or {}).items():
            self._account.credit(coin.upper(), dec(amount))
        self._fees = fee_schedule or FeeSchedule.conservative_default()
        self._account_uid = account_uid
        self._account_mode = account_mode
        self._instruments: dict[str, InstrumentSpec] = {}
        self._books: dict[str, OrderBook] = {}
        self._tickers: dict[str, Ticker] = {}
        self._orders: dict[str, PaperOrder] = {}
        self._executions: list[ExecutionView] = []
        self._positions: dict[tuple[str, str, int], PositionView] = {}
        self._depth_pool: dict[str, _DepthPool] = {}
        self._snapshot_seq = 0
        self._order_seq = 0
        self._exec_seq = 0
        self._last_book_token: dict[str, tuple[dt.datetime, int | None]] = {}
        #: 故障注入：名称 -> 剩余触发次数
        self._faults: dict[str, int] = {}
        self._pending_fills: list[PaperOrder] = []
        self._settlements: dict[str, dict[str, object]] = {}

    # ------------------------------------------------------------------
    # 故障注入（STR-05）
    # ------------------------------------------------------------------

    def inject_fault(self, name: str, times: int = 1) -> None:
        """注入故障。

        支持的故障名：
            ``unknown_result`` —— 下单返回结果未知（对应 EXE-02）
            ``reject``         —— 交易所明确拒绝
            ``partial``        —— 只成交一部分（用 ``partial_ratio`` 控制比例）
            ``network_error``  —— 网络异常
        """
        self._faults[name] = self._faults.get(name, 0) + times

    def _consume_fault(self, name: str) -> bool:
        remaining = self._faults.get(name, 0)
        if remaining <= 0:
            return False
        self._faults[name] = remaining - 1
        return True

    @property
    def partial_ratio(self) -> Decimal:
        return getattr(self, "_partial_ratio", Decimal("1"))

    def set_partial_ratio(self, ratio: Decimal) -> None:
        if not dec(ratio).is_finite() or not 0 < dec(ratio) <= 1:
            raise ValueError("模拟部分成交比例必须在 (0,1] 内")
        self._partial_ratio = dec(ratio)

    # ------------------------------------------------------------------
    # 行情注入
    # ------------------------------------------------------------------

    def set_instrument(self, spec: InstrumentSpec) -> None:
        self._instruments[spec.symbol] = spec

    def set_book(self, book: OrderBook) -> None:
        """注入一份新盘口。

        这是撮合的触发点：STR-05 规定「意图接收之后的下一份有效盘口」为最早
        成交时点，所以只有挂单之后到达的盘口才会撮合它。
        """
        moment = self._clock()
        if (
            book.gap_detected
            or not book.bids
            or not book.asks
            or book.source_ts is None
            or book.received_at is None
            or (moment - book.source_ts).total_seconds() > 15
            or (moment - book.received_at).total_seconds() > 15
            or book.source_ts > moment + dt.timedelta(seconds=5)
            or book.received_at > moment + dt.timedelta(seconds=5)
            or book.bid() >= book.ask()
            or any(
                p <= 0 or q < 0 or not p.is_finite() or not q.is_finite()
                for p, q in (*book.bids, *book.asks)
            )
        ):
            return
        token = (book.source_ts, book.update_id)
        previous = self._last_book_token.get(book.symbol)
        if previous and (token == previous or book.source_ts < previous[0]):
            return
        self._last_book_token[book.symbol] = token
        self._books[book.symbol] = book
        self._snapshot_seq += 1
        self._depth_pool[book.symbol] = _DepthPool(
            snapshot_id=self._snapshot_seq,
            bids=sorted(book.bids, reverse=True),
            asks=sorted(book.asks),
        )
        self._match_resting_orders(book.symbol)

    def set_ticker(self, ticker: Ticker) -> None:
        self._tickers[ticker.symbol] = ticker

    def _match_resting_orders(self, symbol: str) -> None:
        """用当前快照撮合挂单。

        处理顺序固定：按 (创建时间, order_link_id) 排序，保证可重复。
        """
        pool = self._depth_pool.get(symbol)
        if pool is None:
            return
        candidates = sorted(
            (
                o
                for o in self._orders.values()
                if o.request.symbol == symbol
                and o.leaves_qty > 0
                and o.status in ACTIVE_STATUSES
                and self._books[symbol].source_ts > o.created_at
            ),
            key=lambda o: (o.created_at, o.order_link_id),
        )
        for order in candidates:
            wanted = order.leaves_qty
            if self._consume_fault("partial"):
                wanted *= self.partial_ratio
            if wanted <= 0:
                continue
            fills = []
            if order.request.side is OrderSide.BUY:
                if pool.asks and order.request.price is not None:
                    fills = pool.take_asks(order.request.price, wanted)
            elif pool.bids and order.request.price is not None:
                fills = pool.take_bids(order.request.price, wanted)
            for price, qty in fills:
                self._apply_fill(order, price, qty)

    # ------------------------------------------------------------------
    # 订单
    # ------------------------------------------------------------------

    async def create_order(self, request: OrderRequest) -> OrderAck:
        spec = self._instruments.get(request.symbol)
        if request.order_link_id in self._orders:
            raise OrderUncertain("模拟重复物理订单编号，保留原订单")
        if (
            spec is None
            or not spec.is_tradable
            or spec.category != request.category
            or request.order_type is not OrderType.LIMIT
            or request.price is None
            or not request.price.is_finite()
            or request.price <= 0
            or not request.qty.is_finite()
            or request.qty <= 0
            or not request.order_link_id
        ):
            raise OrderRejected("模拟订单缺少有效规格、限价或数量")
        active = [o for o in self._orders.values() if o.status in ACTIVE_STATUSES]
        if request.side is OrderSide.BUY:
            coin = spec.settle_coin if request.category == "option" else spec.quote_coin
            rate = (
                self._fees.option_taker_rate
                if request.category == "option"
                else self._fees.spot_taker_rate
            )
            held = sum(
                (
                    o.leaves_qty * o.request.price * (1 + rate)
                    for o in active
                    if o.request.side is OrderSide.BUY and self._cash_coin(o.request) == coin
                ),
                Decimal("0"),
            )
            if not coin or request.qty * request.price * (1 + rate) > (
                self._account.balance(coin).available - held
            ):
                raise OrderRejected("模拟自有现金不足")
        else:
            pending = sum(
                (
                    o.leaves_qty
                    for o in active
                    if o.request.symbol == request.symbol and o.request.side is OrderSide.SELL
                ),
                Decimal("0"),
            )
            if request.category == "option":
                position = self._positions.get(("option", request.symbol, 0))
                available = position.size if position else Decimal("0")
                if not request.reduce_only or request.position_idx != 0:
                    raise OrderRejected("模拟期权仅允许 reduceOnly 多头减仓")
            else:
                available = self._account.balance(spec.base_coin).available
            if request.qty > available - pending:
                raise OrderRejected("模拟可平数量不足")
        if self._consume_fault("network_error"):
            raise ConnectionError("注入的故障：网络异常")
        if self._consume_fault("unknown_result"):
            # 结果未知：订单实际已经落地，但调用方拿不到回执（EXE-02）。
            order = self._register(request)
            order.status = "New"
            raise OrderUncertain(f"注入的故障：{request.order_link_id} 结果未知")
        if self._consume_fault("reject"):
            raise OrderRejected(
                f"注入的故障：拒绝 {request.order_link_id}", ret_code=110003, ret_msg="injected"
            )

        order = self._register(request)
        return OrderAck(
            order_id=order.order_id,
            order_link_id=request.order_link_id,
            status=order.status,
        )

    def _register(self, request: OrderRequest) -> PaperOrder:
        self._order_seq += 1
        order = PaperOrder(
            order_link_id=request.order_link_id,
            request=request,
            created_at=self._clock(),
            order_id=f"paper-{self._order_seq:08d}",
        )
        self._orders[request.order_link_id] = order
        return order

    def _apply_fill(self, order: PaperOrder, price: Decimal, qty: Decimal) -> None:
        """记账一笔成交。"""
        notional = price * qty
        rate = (
            self._fees.spot_taker_rate
            if order.request.category == "spot"
            else self._fees.option_taker_rate
        )
        fee = notional * rate
        request = order.request

        if request.category == "spot":
            quote = self._instrument_quote(request.symbol)
            base = self._instrument_base(request.symbol)
            if request.side is OrderSide.BUY:
                fee = qty * rate
                fee_asset = base
                self._account.debit(quote, notional)
                self._account.credit(base, qty - fee)
                self._apply_spot_position(request.symbol, request.side.value, qty - fee, price)
            else:
                fee_asset = quote
                self._account.debit(base, qty)
                self._account.credit(quote, notional - fee)
                self._apply_spot_position(request.symbol, request.side.value, qty, price)
        else:
            settle = self._instrument_settle(request.symbol)
            if settle is None:
                raise ValueError("模拟期权缺少结算币")
            fee += qty * self._fees.option_fee_per_contract
            fee_asset = settle
            if request.side is OrderSide.BUY:
                self._account.debit(settle, notional + fee)
            else:
                self._account.credit(settle, notional - fee)
            self._apply_option_position(request.symbol, request.side.value, qty, price)

        prev_notional = order.avg_price * order.qty_filled
        order.qty_filled += qty
        order.avg_price = (prev_notional + notional) / order.qty_filled
        order.status = "Filled" if order.leaves_qty <= 0 else "PartiallyFilled"

        self._exec_seq += 1
        execution = ExecutionView(
            exec_id=f"paper-exec-{self._exec_seq:08d}",
            order_id=order.order_id,
            order_link_id=order.order_link_id,
            category=request.category,
            symbol=request.symbol,
            side=request.side.value,
            exec_price=price,
            exec_qty=qty,
            exec_fee=fee,
            exec_fee_asset=fee_asset,
            is_maker=False,
            exec_time=self._clock(),
        )
        order.executions.append(execution)
        self._executions.append(execution)

    def _apply_spot_position(self, symbol: str, side: str, qty: Decimal, price: Decimal) -> None:
        key = ("spot", symbol, 0)
        current = self._positions.get(key)
        size = current.size if current else Decimal("0")
        avg = current.avg_price if current and current.avg_price else Decimal("0")
        if side.lower() == "buy":
            new_size = size + qty
            new_avg = (avg * size + price * qty) / new_size if new_size > 0 else Decimal("0")
        else:
            new_size = size - qty
            new_avg = avg
        self._positions[key] = PositionView(
            category="spot",
            symbol=symbol,
            position_idx=0,
            size=new_size,
            side="Buy" if new_size > 0 else None,
            avg_price=new_avg,
            raw={"simulated": True},
        )

    def _apply_option_position(self, symbol: str, side: str, qty: Decimal, price: Decimal) -> None:
        key = ("option", symbol, 0)
        current = self._positions.get(key)
        size = current.size if current else Decimal("0")
        avg = current.avg_price if current and current.avg_price else Decimal("0")
        if side.lower() == "buy":
            new_size = size + qty
            new_avg = (avg * size + price * qty) / new_size if new_size > 0 else Decimal("0")
        else:
            new_size = size - qty
            new_avg = avg
        self._positions[key] = PositionView(
            category="option",
            symbol=symbol,
            position_idx=0,
            size=new_size,
            side="Buy" if new_size > 0 else None,
            avg_price=new_avg,
            settle_coin=self._instrument_settle(symbol),
            delivery_time=self._instruments[symbol].delivery_time,
            raw={"simulated": True},
        )

    async def cancel_order(
        self,
        category: str,
        symbol: str,
        *,
        order_id: str | None = None,
        order_link_id: str | None = None,
    ) -> CancelAck:
        order = None
        if order_link_id is not None:
            order = self._orders.get(order_link_id)
        elif order_id is not None:
            order = next((o for o in self._orders.values() if o.order_id == order_id), None)
        if order is None or order.request.category != category or order.request.symbol != symbol:
            raise OrderRejected("撤单失败：找不到订单", ret_code=110001, ret_msg="order not exist")
        if self._consume_fault("cancel_unknown"):
            raise OrderUncertain("模拟撤单结果未知，原订单保持活动")
        if order.leaves_qty > 0 and order.status in ACTIVE_STATUSES:
            order.status = "Cancelled"
        return CancelAck(
            order_id=order.order_id,
            order_link_id=order.order_link_id,
            accepted=True,
        )

    # ------------------------------------------------------------------
    # 只读
    # ------------------------------------------------------------------

    async def get_server_time(self) -> dt.datetime:
        return now_utc()

    async def get_instruments(self, category: str) -> list[InstrumentSpec]:
        return [s for s in self._instruments.values() if s.category == category]

    async def get_ticker(self, category: str, symbol: str) -> Ticker:
        ticker = self._tickers.get(symbol)
        if ticker is not None:
            return ticker
        book = self._books.get(symbol)
        if book is None:
            return Ticker(category=category, symbol=symbol)
        return Ticker(
            category=category,
            symbol=symbol,
            bid_price=book.bid(),
            bid_size=book.bids[0][1] if book.bids else None,
            ask_price=book.ask(),
            ask_size=book.asks[0][1] if book.asks else None,
            source_ts=book.source_ts,
            received_at=book.received_at,
        )

    async def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> OrderBook:
        book = self._books.get(symbol)
        if book is None:
            return OrderBook(category=category, symbol=symbol)
        return OrderBook(
            category=category,
            symbol=symbol,
            bids=book.bids[:limit],
            asks=book.asks[:limit],
            source_ts=book.source_ts,
            received_at=book.received_at,
            update_id=book.update_id,
        )

    async def get_account(self) -> AccountView:
        return AccountView(
            account_uid=self._account_uid,
            account_mode=self._account_mode,
            coins=dict(self._account.coins),
            raw={"simulated": True, "fee_version": self._fees.version},
            source_ts=self._clock(),
            received_at=self._clock(),
        )

    async def get_positions(self, category: str) -> list[PositionView]:
        return [p for (cat, _sym, _idx), p in self._positions.items() if cat == category]

    async def get_open_orders(self, category: str) -> list[OrderView]:
        return [
            self._to_view(o)
            for o in self._orders.values()
            if o.request.category == category and o.status in ACTIVE_STATUSES
        ]

    async def get_order_history(
        self, category: str, *, order_link_ids: list[str] | None = None
    ) -> list[OrderView]:
        result = []
        for order in self._orders.values():
            if order.request.category != category:
                continue
            if order_link_ids and order.order_link_id not in order_link_ids:
                continue
            result.append(self._to_view(order))
        return result

    async def get_executions(
        self,
        category: str,
        *,
        order_link_ids: list[str] | None = None,
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
    ) -> list[ExecutionView]:
        return [
            e
            for e in self._executions
            if e.category == category
            and (not order_link_ids or e.order_link_id in order_link_ids)
            and (start_time is None or e.exec_time >= start_time)
            and (end_time is None or e.exec_time <= end_time)
        ]

    async def get_key_info(self) -> KeyInfo:
        return KeyInfo(
            api_key_prefix="paper***",
            permissions=frozenset({"Read"}),
            ip_restrictions=(),
            read_only=True,
        )

    async def order_updates(self, category: str) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - 纸面不产生推送

    async def execution_updates(self, category: str) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - 纸面不产生推送

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _cash_coin(self, request: OrderRequest) -> str | None:
        spec = self._instruments[request.symbol]
        return spec.settle_coin if request.category == "option" else spec.quote_coin

    def settle_option(
        self,
        *,
        settlement_id: str,
        symbol: str,
        settlement_price: Decimal,
        occurred_at: dt.datetime,
        reference: str,
        fee: Decimal = Decimal("0"),
    ) -> dict[str, object]:
        """显式的模拟交割事实；时间经过或持仓消失不会自动生成支付。"""
        price, fee = dec(settlement_price), dec(fee)
        if (
            not settlement_id
            or not reference
            or occurred_at.tzinfo is None
            or not price.is_finite()
            or price <= 0
            or not fee.is_finite()
            or fee < 0
            or occurred_at > self._clock()
        ):
            raise ValueError("模拟结算价格、时间或证据无效")
        existing = self._settlements.get(settlement_id)
        if existing:
            if (
                existing["symbol"] != symbol
                or existing["settlement_price"] != str(price)
                or existing["occurred_at"] != occurred_at.isoformat()
                or existing["fee"] != str(fee)
            ):
                raise ValueError("模拟交割编号事实冲突")
            return dict(existing)
        spec = self._instruments.get(symbol)
        position = self._positions.get(("option", symbol, 0))
        if (
            spec is None
            or spec.category != "option"
            or not spec.settle_coin
            or spec.delivery_time is None
            or occurred_at < spec.delivery_time
            or spec.strike_price is None
            or spec.options_type not in {"Call", "Put"}
            or position is None
            or position.side != "Buy"
            or position.size <= 0
            or any(
                o.request.symbol == symbol and o.status in ACTIVE_STATUSES
                for o in self._orders.values()
            )
        ):
            raise ValueError("模拟交割缺少已到期多头或仍有活动订单")
        intrinsic = max(
            Decimal("0"),
            price - spec.strike_price if spec.options_type == "Call" else spec.strike_price - price,
        )
        payout = intrinsic * position.size
        if payout < fee and self._account.balance(spec.settle_coin).available < fee - payout:
            raise ValueError("模拟交割费用现金不足")
        event = {
            "settlement_id": settlement_id,
            "symbol": symbol,
            "currency": spec.settle_coin,
            "qty": str(position.size),
            "payout": str(payout),
            "fee": str(fee),
            "settlement_price": str(price),
            "occurred_at": occurred_at.isoformat(),
            "reference": reference,
        }
        self._account.credit(spec.settle_coin, payout)
        self._account.debit(spec.settle_coin, fee)
        self._positions[("option", symbol, 0)] = PositionView(
            category="option",
            symbol=symbol,
            size=Decimal("0"),
            side="Buy",
            settle_coin=spec.settle_coin,
            delivery_time=spec.delivery_time,
        )
        self._settlements[settlement_id] = event
        return dict(event)

    def settlements(self) -> list[dict[str, object]]:
        return [dict(value) for value in self._settlements.values()]

    def export_state(self) -> dict[str, object]:
        """可审计 JSON；不依赖 pickle，也不在重启时重新充值初始余额。"""
        state = {
            "version": 1,
            "environment": self.environment,
            "account_uid": self._account_uid,
            "account_mode": self._account_mode,
            "fees": asdict(self._fees),
            "coins": [asdict(c) for c in self._account.coins.values()],
            "instruments": [asdict(s) for s in self._instruments.values()],
            "positions": [asdict(p) for p in self._positions.values()],
            "executions": [asdict(e) for e in self._executions],
            "orders": [
                {
                    "request": asdict(o.request),
                    "created_at": o.created_at,
                    "qty_filled": o.qty_filled,
                    "avg_price": o.avg_price,
                    "status": o.status,
                    "order_id": o.order_id,
                }
                for o in self._orders.values()
            ],
            "counters": [self._snapshot_seq, self._order_seq, self._exec_seq],
            "last_book_tokens": {
                key: [value[0].isoformat(), value[1]]
                for key, value in self._last_book_token.items()
            },
            "faults": self._faults,
            "partial_ratio": self.partial_ratio,
            "settlements": self._settlements,
        }
        return json.loads(
            json.dumps(
                state, default=lambda v: v.isoformat() if isinstance(v, dt.datetime) else str(v)
            )
        )

    def restore_state(self, state: dict[str, object]) -> None:
        if state.get("version") != 1 or state.get("environment") != "paper":
            raise ValueError("模拟检查点版本或环境不匹配")
        if state.get("account_uid") != self._account_uid:
            raise ValueError("模拟检查点账户不匹配")
        fees = _restore_dataclass(FeeSchedule, state["fees"])
        if fees != self._fees:
            raise ValueError("模拟检查点费用版本不匹配，须审计后迁移")
        coins = [_restore_dataclass(CoinBalance, row) for row in state["coins"]]
        specs = [_restore_dataclass(InstrumentSpec, row) for row in state["instruments"]]
        positions = [_restore_dataclass(PositionView, row) for row in state["positions"]]
        executions = [_restore_dataclass(ExecutionView, row) for row in state["executions"]]
        orders = {}
        for row in state["orders"]:
            request = _restore_dataclass(OrderRequest, row["request"])
            orders[request.order_link_id] = PaperOrder(
                request.order_link_id,
                request,
                _timestamp(row["created_at"]),
                dec(row["qty_filled"]),
                dec(row["avg_price"]),
                row["status"],
                row["order_id"],
                [e for e in executions if e.order_link_id == request.order_link_id],
            )
        counters = state["counters"]
        if len(counters) != 3 or any(type(c) is not int or c < 0 for c in counters):
            raise ValueError("模拟检查点计数器无效")
        self._account.coins = {c.coin: c for c in coins}
        self._instruments = {s.symbol: s for s in specs}
        self._positions = {(p.category, p.symbol, p.position_idx): p for p in positions}
        self._executions, self._orders = executions, orders
        self._snapshot_seq, self._order_seq, self._exec_seq = counters
        self._last_book_token = {
            key: (_timestamp(token[0]), token[1])
            for key, token in state["last_book_tokens"].items()
        }
        self._faults = {key: int(value) for key, value in state["faults"].items()}
        self.set_partial_ratio(dec(state["partial_ratio"]))
        self._settlements = {
            key: dict(value) for key, value in state.get("settlements", {}).items()
        }
        self._books.clear()
        self._depth_pool.clear()
        self._tickers.clear()

    def _to_view(self, order: PaperOrder) -> OrderView:
        return OrderView(
            order_id=order.order_id,
            order_link_id=order.order_link_id,
            category=order.request.category,
            symbol=order.request.symbol,
            side=order.request.side.value,
            order_type=order.request.order_type.value,
            price=order.request.price,
            qty=order.request.qty,
            qty_filled=order.qty_filled,
            status=order.status,
            time_in_force=order.request.time_in_force.value,
            reduce_only=order.request.reduce_only,
            position_idx=order.request.position_idx,
            avg_price=order.avg_price if order.qty_filled > 0 else None,
            created_at=order.created_at,
            updated_at=now_utc(),
            raw={"simulated": True},
        )

    def _instrument_base(self, symbol: str) -> str:
        spec = self._instruments.get(symbol)
        return spec.base_coin if spec else symbol

    def _instrument_quote(self, symbol: str) -> str:
        spec = self._instruments.get(symbol)
        return spec.quote_coin if spec else "USDT"

    def _instrument_settle(self, symbol: str) -> str | None:
        spec = self._instruments.get(symbol)
        return spec.settle_coin if spec else None


_DECIMAL_FIELDS = frozenset(
    {
        "equity",
        "available",
        "wallet_balance",
        "usd_value",
        "borrowed",
        "accrued_interest",
        "locked",
        "tick_size",
        "qty_step",
        "min_order_qty",
        "max_order_qty",
        "min_order_amt",
        "max_order_amt",
        "strike_price",
        "size",
        "avg_price",
        "mark_price",
        "delta",
        "gamma",
        "vega",
        "theta",
        "unrealised_pnl",
        "realised_pnl",
        "exec_price",
        "exec_qty",
        "exec_fee",
        "qty",
        "price",
        "spot_taker_rate",
        "option_taker_rate",
        "option_fee_per_contract",
    }
)
_TIME_FIELDS = frozenset({"delivery_time", "exec_time"})


def _timestamp(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("模拟检查点时间缺少时区")
    return stamp


def _restore_dataclass(cls, row):
    values = dict(row)
    for name, value in values.items():
        if value is None:
            continue
        if name in _DECIMAL_FIELDS:
            values[name] = dec(value)
            if not values[name].is_finite():
                raise ValueError("模拟检查点金额无效")
        elif name in _TIME_FIELDS:
            values[name] = _timestamp(value)
    if cls is OrderRequest:
        values["side"] = OrderSide(values["side"])
        values["order_type"] = OrderType(values["order_type"])
        values["time_in_force"] = TimeInForce(values["time_in_force"])
    return cls(**values)


__all__ = ["FeeSchedule", "PaperAdapter", "PaperFault"]
