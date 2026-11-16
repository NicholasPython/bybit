"""交易适配器接口。

需求约束（§3、§4.5）：
    * 策略不能直接使用交易密钥或调用交易写接口；
    * 管理页面提交控制命令，执行仍经过统一校验；
    * Web 服务重启不得自动创建第二个交易执行者。

因此核心逻辑只依赖本模块定义的抽象接口，具体实现有：
    * :class:`~bybitbot.adapters.bybit_rest.BybitRestAdapter` —— 真实交易所
    * :class:`~bybitbot.adapters.paper.PaperAdapter` —— 确定性模拟撮合

两者行为一致，才能让同一套验收用例在纸面与联调环境复用。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from bybitbot.settings.models import OrderSide, OrderType, TimeInForce

#: 交易所「重复 orderLinkId」一类错误码。
#:
#: EXE-02 明确：重复编号报错**本身不是未受理的证明** —— 它可能说明订单
#: 早就存在。因此这类错误必须按「结果未知」处理，不能释放预留。
#: 「orderLinkId 重复」类错误码。
#:
#: Bybit V5 官方错误码表：``110072`` = OrderLinkedID is duplicate（UTA），
#: ``170141`` = Duplicate clientOrderId（现货）。
#:
#: 注意 ``110003/110004/110007/110012`` **不是**重复编号 —— 它们分别是
#: 价格越界与余额不足，重试与否的处理完全不同。
DUPLICATE_ORDER_LINK_RETCODES: frozenset[int] = frozenset({110072, 170141})


class AdapterError(Exception):
    """适配器层错误基类。"""


class TransientError(AdapterError):
    """可重试的网络/限流错误（仅查询允许重试，EXE-04）。"""


class RateLimited(TransientError):
    """被交易所限流。"""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OrderRejected(AdapterError):
    """交易所明确拒绝。

    EXE-02：明确拒绝是「可以消除不确定性」的证据之一，因此它必须携带
    交易所的原始 retCode，便于审计。
    """

    def __init__(self, message: str, *, ret_code: int = 0, ret_msg: str = "") -> None:
        super().__init__(message)
        self.ret_code = ret_code
        self.ret_msg = ret_msg


class OrderUncertain(AdapterError):
    """请求结果不确定（超时、响应丢失等）。

    EXE-02：进入 ``UNKNOWN``，保留预留并暂停账户新增风险。
    """


class InsufficientCapability(AdapterError):
    """账户/环境不具备该能力（如 Demo 不支持期权减仓）。

    需求文档要求：不支持项记录为未通过，不能以现货成功替代期权验收。
    """


# --------------------------------------------------------------------------
# 领域快照
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentSpec:
    """合约规格（MKT-01）。字段缺失时上层必须拒绝相关交易。"""

    category: Literal["spot", "option"]
    symbol: str
    base_coin: str
    quote_coin: str
    status: str
    tick_size: Decimal | None = None
    qty_step: Decimal | None = None
    min_order_qty: Decimal | None = None
    max_order_qty: Decimal | None = None
    min_order_amt: Decimal | None = None
    max_order_amt: Decimal | None = None
    settle_coin: str | None = None
    options_type: Literal["Call", "Put"] | None = None
    strike_price: Decimal | None = None
    delivery_time: dt.datetime | None = None
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def is_tradable(self) -> bool:
        return self.status.lower() == "trading"


@dataclass(frozen=True)
class Ticker:
    """行情快照（MKT-02）。"""

    category: str
    symbol: str
    bid_price: Decimal | None = None
    bid_size: Decimal | None = None
    ask_price: Decimal | None = None
    ask_size: Decimal | None = None
    last_price: Decimal | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    open_interest: Decimal | None = None
    #: 期权隐含波动率（MKT-03：缺失或未验证单位时相关规则不执行）。
    iv: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    vega: Decimal | None = None
    theta: Decimal | None = None
    delivery_time: dt.datetime | None = None
    source_ts: dt.datetime | None = None
    received_at: dt.datetime | None = None

    @property
    def is_crossed(self) -> bool:
        """交叉报价：买价高于卖价，属于异常数据，不得用于批准订单。"""
        if self.bid_price is None or self.ask_price is None:
            return False
        return self.bid_price > self.ask_price


@dataclass(frozen=True)
class OrderBook:
    """盘口快照（MKT-02）。

    ``bids`` / ``asks`` 为 (价格, 数量) 列表，按价格优先级排列。
    """

    category: str
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...] = ()
    asks: tuple[tuple[Decimal, Decimal], ...] = ()
    source_ts: dt.datetime | None = None
    received_at: dt.datetime | None = None
    update_id: int | None = None
    #: 增量推送出现断档时置位；重建完成前不得使用不完整数据（MKT-02）。
    gap_detected: bool = False

    def bid(self, level: int = 0) -> Decimal | None:
        return self.bids[level][0] if len(self.bids) > level else None

    def ask(self, level: int = 0) -> Decimal | None:
        return self.asks[level][0] if len(self.asks) > level else None


@dataclass(frozen=True)
class CoinBalance:
    """单一币种的余额（来自 wallet-balance）。

    ``equity`` 是该币种的总权益；``available`` 是可用；``borrowed`` 是负债
    —— 负债不为零说明发生了 UTA 自动借贷，必须暂停账户（RSK-02 / OPT-03）。
    """

    coin: str
    equity: Decimal
    available: Decimal
    wallet_balance: Decimal | None = None
    usd_value: Decimal | None = None
    borrowed: Decimal = Decimal("0")
    accrued_interest: Decimal = Decimal("0")
    locked: Decimal = Decimal("0")

    @property
    def has_debt(self) -> bool:
        return self.borrowed > 0 or self.accrued_interest > 0


@dataclass(frozen=True)
class AccountView:
    """账户视图（只读）。"""

    account_uid: str
    account_mode: str
    coins: dict[str, CoinBalance] = field(default_factory=dict)
    total_equity: Decimal | None = None
    #: 交易所自报的权益，仅用于核对风控权益，不叠加期权价值（LED-04）。
    exchange_equity: Decimal | None = None
    raw: dict[str, object] = field(default_factory=dict)
    source_ts: dt.datetime | None = None
    received_at: dt.datetime | None = None

    def coin(self, name: str) -> CoinBalance:
        return self.coins.get(
            name.upper(),
            CoinBalance(coin=name.upper(), equity=Decimal("0"), available=Decimal("0")),
        )

    @property
    def has_debt(self) -> bool:
        return any(c.has_debt for c in self.coins.values())


@dataclass(frozen=True)
class PositionView:
    """持仓视图。"""

    category: str
    symbol: str
    position_idx: int = 0
    size: Decimal = Decimal("0")
    side: str | None = None
    avg_price: Decimal | None = None
    mark_price: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    vega: Decimal | None = None
    theta: Decimal | None = None
    unrealised_pnl: Decimal | None = None
    realised_pnl: Decimal | None = None
    settle_coin: str | None = None
    delivery_time: dt.datetime | None = None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderView:
    """交易所订单快照。"""

    order_id: str | None
    order_link_id: str
    category: str
    symbol: str
    side: str
    order_type: str
    status: str
    price: Decimal | None
    qty: Decimal
    qty_filled: Decimal = Decimal("0")
    time_in_force: str | None = None
    reduce_only: bool = False
    position_idx: int | None = None
    avg_price: Decimal | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    reject_reason: str | None = None
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def leaves_qty(self) -> Decimal:
        return self.qty - self.qty_filled


@dataclass(frozen=True)
class ExecutionView:
    """交易所成交快照。"""

    exec_id: str
    order_id: str | None
    order_link_id: str | None
    category: str
    symbol: str
    side: str
    exec_price: Decimal
    exec_qty: Decimal
    exec_time: dt.datetime
    exec_fee: Decimal | None = None
    exec_fee_asset: str | None = None
    is_maker: bool | None = None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionDeliveryView:
    """Corroborated option-expiry fact; account/position reconciliation is separate.

    ``payout`` is gross intrinsic value in ``currency``; ``fee`` is the delivery
    cash expense, not entry premium or previously paid trading fees. Evidence
    contains only allowlisted fields from the documented delivery endpoints.
    """

    settlement_id: str
    symbol: str
    side: str
    qty: Decimal
    currency: str
    payout: Decimal
    fee: Decimal
    occurred_at: dt.datetime
    delivery_price: Decimal
    strike_price: Decimal
    reference: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class KeyInfo:
    """API Key 身份与权限摘要（ENV-02 / SEC-01）。

    启动时只记录脱敏身份和权限摘要。
    """

    api_key_prefix: str
    permissions: frozenset[str]
    ip_restrictions: tuple[str, ...] = ()
    read_only: bool = False
    #: 密钥是否带有提现权限（SEC-01 要求关闭提现）。带提现权限的密钥
    #: 不能用于本系统的实盘启动。
    withdraw_enabled: bool = False
    account_uid: str | None = None


# --------------------------------------------------------------------------
# 写请求
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRequest:
    """下单请求。

    ``order_link_id`` 是幂等键，必须唯一且符合交易所长度限制（EXE-01）。
    """

    category: Literal["spot", "option"]
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    order_link_id: str = ""
    reduce_only: bool = False
    position_idx: int | None = None
    close_on_trigger: bool = False
    coverage_symbol: str | None = None


@dataclass(frozen=True)
class OrderAck:
    """下单应答。"""

    order_id: str | None
    order_link_id: str
    status: str
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CancelAck:
    """撤单应答。

    撤单受理不等于撤单完成（EXE-03）；必须核对最终订单状态和累计成交后
    才释放剩余预留。
    """

    order_id: str | None
    order_link_id: str
    accepted: bool
    raw: dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 推送事件
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderUpdate:
    """订单状态推送。"""

    order_id: str | None
    order_link_id: str
    category: str
    symbol: str
    status: str
    qty_filled: Decimal
    qty: Decimal
    price: Decimal | None = None
    event_time: dt.datetime | None = None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionUpdate:
    """成交推送。"""

    exec_id: str
    order_id: str | None
    order_link_id: str | None
    category: str
    symbol: str
    side: str
    exec_price: Decimal
    exec_qty: Decimal
    exec_fee: Decimal | None = None
    exec_fee_asset: str | None = None
    is_maker: bool | None = None
    event_time: dt.datetime | None = None
    raw: dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 适配器协议
# --------------------------------------------------------------------------


@runtime_checkable
class TradingAdapter(Protocol):
    """核心逻辑依赖的全部交易所能力。

    实现者必须保证：写请求**不做通用自动重试**（EXE-04），
    写请求超时必须抛 :class:`OrderUncertain` 而不是 :class:`TransientError`。
    """

    #: 环境标识（paper/demo/testnet/live）。
    environment: str
    #: 是否允许发送交易写请求。Paper 必须为 False。
    allow_trade_writes: bool

    # ---- 只读 ----
    async def get_server_time(self) -> dt.datetime: ...

    async def get_instruments(self, category: str) -> list[InstrumentSpec]: ...

    async def get_ticker(self, category: str, symbol: str) -> Ticker: ...

    async def get_klines(self, symbol: str, interval: str, *, end_ms: int, limit: int) -> list: ...

    async def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> OrderBook: ...

    async def get_account(self) -> AccountView: ...

    async def get_positions(self, category: str) -> list[PositionView]: ...

    async def get_open_orders(self, category: str) -> list[OrderView]: ...

    async def get_order_history(
        self, category: str, *, order_link_ids: list[str] | None = None
    ) -> list[OrderView]: ...

    async def get_executions(
        self,
        category: str,
        *,
        order_link_ids: list[str] | None = None,
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
    ) -> list[ExecutionView]: ...

    async def get_option_deliveries(
        self,
        spec: InstrumentSpec,
        *,
        start_time: dt.datetime,
        end_time: dt.datetime,
    ) -> list[OptionDeliveryView]: ...

    async def get_key_info(self) -> KeyInfo: ...

    # ---- 写 ----
    async def create_order(self, request: OrderRequest) -> OrderAck: ...

    async def cancel_order(
        self,
        category: str,
        symbol: str,
        *,
        order_id: str | None = None,
        order_link_id: str | None = None,
    ) -> CancelAck: ...

    # ---- 推送 ----
    def order_updates(self, category: str) -> AsyncIterator[OrderUpdate]: ...

    def execution_updates(self, category: str) -> AsyncIterator[ExecutionUpdate]: ...


__all__ = [
    "DUPLICATE_ORDER_LINK_RETCODES",
    "AccountView",
    "AdapterError",
    "CancelAck",
    "CoinBalance",
    "ExecutionUpdate",
    "ExecutionView",
    "InsufficientCapability",
    "InstrumentSpec",
    "KeyInfo",
    "OptionDeliveryView",
    "OrderAck",
    "OrderBook",
    "OrderRejected",
    "OrderRequest",
    "OrderUncertain",
    "OrderUpdate",
    "OrderView",
    "PositionView",
    "RateLimited",
    "Ticker",
    "TradingAdapter",
    "TransientError",
]
