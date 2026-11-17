"""数据库模型 —— 对应需求文档 §5.1「主要数据实体」。

约束（需求文档 §5.1）：
    * 金额和数量使用 Decimal/数据库 DECIMAL；
    * 时间使用带时区的 UTC 时间戳；
    * 数据库唯一约束保障信号、物理订单、成交和控制命令的幂等性；
    * 业务账本追加事件，纠错使用更正事件（LED-01）。

去重域（LED-01）：账户/环境/产品 + 成交编号。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    """所有表的基类。"""

    type_annotation_map = {dict[str, object]: JSONB}


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ==========================================================================
# 环境与账户
# ==========================================================================


class AccountSnapshot(Base):
    """账户余额快照（LED-02 对账来源）。

    ``complete=False`` 表示这份快照不足以支撑风控判断；此时必须保持暂停
    （MKT-03 / RSK-03）。
    """

    __tablename__ = "account_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    account_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    coins: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    raw: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    complete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("environment", "account_uid", "captured_at", name="uq_account_snap"),
    )


class RiskPause(Base):
    """暂停记录（OPS-03）。

    区分「技术暂停」与「锁定暂停」：亏损熔断、未知订单、外部改仓造成的
    暂停必须保持锁定，重启与换日都不能解除（LED-04 / OPS-03）。
    """

    __tablename__ = "risk_pause"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # ACCOUNT / UNDERLYING
    scope_key: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: True = 锁定暂停，只能由对应解除规则或审计化的人工操作清除。
    locked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 注意：这里不能加 index=True —— 那会生成一个同名的单列索引
    # ``ix_risk_pause_active``，与下面的复合索引重名并把复合索引挤掉。
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        Index("ix_risk_pause_active", "environment", "scope", "scope_key", "active"),
    )


# ==========================================================================
# 合约目录与行情（MKT-01 / MKT-02）
# ==========================================================================


class Instrument(Base):
    """合约规格缓存（MKT-01）。

    交易规则随合约变化，订单必须按这里的最新步长校验；字段缺失时拒绝交易。
    """

    __tablename__ = "instrument"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)  # spot / option
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    base_coin: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_coin: Mapped[str] = mapped_column(String(16), nullable=False)
    settle_coin: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    qty_step: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    min_order_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    max_order_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    min_order_amt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    max_order_amt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    #: 期权专用
    options_type: Mapped[str | None] = mapped_column(String(16))
    strike_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    delivery_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "category", "symbol", name="uq_instrument"),
    )


class QuoteSnapshot(Base):
    """行情快照（STR-01 决策引用的数据快照）。

    保存来源时间、接收时间与数据有效性；过期或交叉报价时暂停开仓（MKT-02）。
    """

    __tablename__ = "quote_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    bid_size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    ask_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    ask_size: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    index_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    #: 交叉报价等异常标记；True 时该快照不可用于批准订单。
    anomalous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_quote_lookup", "environment", "category", "symbol", "received_at"),
    )


# ==========================================================================
# 策略与配置版本（STR-01 / STR-04）
# ==========================================================================


class Strategy(Base):
    """策略实例。"""

    __tablename__ = "strategy"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    versions: Mapped[list[StrategyConfigVersion]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("environment", "name", name="uq_strategy_name"),
    )


class StrategyConfigVersion(Base):
    """不可变配置版本（STR-04）。

    正在处理的订单保留其原始决策版本，因此这里只追加、不更新。
    """

    __tablename__ = "strategy_config_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategy.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    exit_rules: Mapped[list[object]] = mapped_column(JSONB, default=list)
    symbols: Mapped[list[str]] = mapped_column(JSONB, default=list)
    schedule_utc: Mapped[list[str]] = mapped_column(JSONB, default=list)
    budget_per_run: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    max_spend_per_trade: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    strategy: Mapped[Strategy] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),
    )


# ==========================================================================
# 调度与信号（STR-02）
# ==========================================================================


class ScheduleRun(Base):
    """持久化调度记录（STR-02）。

    ``dedupe_key`` 由策略实例 + 规则编号 + 计划时刻构成，是幂等的唯一依据；
    任务重试与进程重启都不会重复执行。
    """

    __tablename__ = "schedule_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategy.id", ondelete="CASCADE"), nullable=False
    )
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "dedupe_key", name="uq_schedule_dedupe"),
        Index("ix_schedule_lookup", "environment", "strategy_id", "planned_at"),
    )


# ==========================================================================
# 交易意图与风控决策（STR-01 / RSK-01）
# ==========================================================================


class TradeIntent(Base):
    """标准交易意图（STR-01）。

    策略只产出这个对象；执行与风控都基于它，策略本身不持有交易密钥。
    """

    __tablename__ = "trade_intent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    account_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy.id", ondelete="SET NULL")
    )
    strategy_version: Mapped[int | None] = mapped_column(Integer)
    validation_position_id: Mapped[int | None] = mapped_column(
        ForeignKey("validation_position.id")
    )
    coverage_symbol: Mapped[str | None] = mapped_column(String(64))
    signal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    qty_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    price_limit: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    spend_limit: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 决定该意图要买/卖的数量；减仓意图据此校验可平量。
    reduce_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    #: 期权意图的到期时间与标的，用于到期窗口与集中度检查（OPT-03 / RSK-05）。
    underlying: Mapped[str | None] = mapped_column(String(32))
    delivery_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 行权价（期权意图）。
    strike_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    #: 期权类型 Call / Put。
    options_type: Mapped[str | None] = mapped_column(String(8))
    exit_rules: Mapped[list[object]] = mapped_column(JSONB, default=list)
    quote_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("quote_snapshot.id", ondelete="SET NULL")
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "signal_id", name="uq_intent_signal"),
        Index("ix_intent_strategy", "environment", "strategy_id", "created_at"),
    )


class RiskDecision(Base):
    """风控决策记录（RSK-01）。

    每次检查都输出批准或拒绝、原因和使用的配置版本。
    """

    __tablename__ = "risk_decision"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    intent_id: Mapped[int] = mapped_column(
        ForeignKey("trade_intent.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # APPROVED / REJECTED
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    checks: Mapped[list[object]] = mapped_column(JSONB, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (Index("ix_risk_decision_intent", "intent_id"),)


# ==========================================================================
# 预留（RSK-02）
# ==========================================================================


class Reservation(Base):
    """资金或持仓数量的原子预留。

    RSK-02：资金与可平仓数量必须在同一持久化事务中校验并预留；
    计入有效挂单、部分成交剩余量及结果未知的请求。
    """

    __tablename__ = "reservation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_id: Mapped[int] = mapped_column(
        ForeignKey("trade_intent.id", ondelete="CASCADE"), nullable=False
    )
    #: CASH = 现金额度；INVENTORY = 可平仓数量
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    consumed: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    released: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_reservation_amount"),
        Index("ix_reservation_open", "environment", "kind", "status"),
    )


# ==========================================================================
# 订单与成交（EXE-01 / EXE-03）
# ==========================================================================


class Order(Base):
    """物理订单（EXE-01）。

    每个物理订单有唯一 ``order_link_id``；订单已被受理不代表成交。
    """

    __tablename__ = "order"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_id: Mapped[int] = mapped_column(
        ForeignKey("trade_intent.id", ondelete="CASCADE"), nullable=False
    )
    order_link_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    qty_filled: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    position_idx: Mapped[int | None] = mapped_column(Integer)
    #: 追价重建产生的子订单指向原意图（EXE-04）。
    parent_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("order.id", ondelete="SET NULL")
    )
    rebuild_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 同步可信度，与业务状态分开保存（EXE-05 末段）。
    #: NONE=从未交给交易所；PENDING=已发出待确认；CONFIRMED=交易所事实已确认；
    #: UNCERTAIN=结果不确定（UNKNOWN）。
    sync_state: Mapped[str] = mapped_column(
        String(16), default="NONE", nullable=False
    )
    #: 本地订单 TTL（EXE-04：本地 TTL 到期只触发撤单，不代表交易所终态）。
    local_ttl_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 该订单占用的预留。
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservation.id", ondelete="SET NULL")
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "order_link_id", name="uq_order_link_id"),
        Index("ix_order_status", "environment", "status"),
        Index("ix_order_intent", "intent_id"),
    )


class Execution(Base):
    """成交记录（EXE-03 / LED-01）。

    去重域：环境 + 产品 + 交易所成交编号（LED-01）。
    """

    __tablename__ = "execution"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange_exec_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    order_link_id: Mapped[str | None] = mapped_column(String(64))
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("order.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    exec_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    exec_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    exec_fee: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    exec_fee_asset: Mapped[str | None] = mapped_column(String(16))
    is_maker: Mapped[bool | None] = mapped_column(Boolean)
    exec_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    raw: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "environment", "category", "exchange_exec_id", name="uq_execution_dedupe"
        ),
        Index("ix_execution_order", "order_id"),
    )


# ==========================================================================
# 库存与持仓（SPT-02 / OPT-02 / LED-01）
# ==========================================================================


class Inventory(Base):
    """策略归属库存（SPT-02 / OPT-02）。

    与账户实际持仓分开：账户实际数量由 :class:`Position` 表示，本表只记
    bot 认领的部分，外部改仓不会被自动吸收。
    """

    __tablename__ = "inventory"

    position_side: Mapped[str] = mapped_column(
        String(8), default="Buy", server_default="Buy", nullable=False
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategy.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    avg_cost: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    #: False = 该归属不可信，不能用于授权减仓（OPT-02）。
    trusted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 外部导入的库存须记录成本来源（LED-03）。
    cost_source: Mapped[str | None] = mapped_column(String(64))
    #: 库存从零变为正数的真实成交；完整平仓后下一笔买入重新计时。
    episode_started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    episode_intent_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_intent.id", ondelete="SET NULL")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "environment", "strategy_id", "symbol", "position_side", name="uq_inventory"
        ),
        CheckConstraint("qty >= 0", name="ck_inventory_qty"),
        CheckConstraint("position_side IN ('Buy', 'Sell')", name="ck_inventory_side"),
    )


class Position(Base):
    """账户实际持仓（现货余额与期权仓位）。

    ``trusted=False`` 表示该持仓来源不可信，不得用于授权减仓（RSK-04）。
    """

    __tablename__ = "position"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    position_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    side: Mapped[str | None] = mapped_column(String(8))
    size: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    #: Greeks（每张/每标的单位，映射必须用样例验证 —— RSK-05）。
    delta: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    gamma: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    vega: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    theta: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    unrealised_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    realised_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    settle_coin: Mapped[str | None] = mapped_column(String(16))
    delivery_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    trusted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "environment", "category", "symbol", "position_idx", name="uq_position"
        ),
        Index("ix_position_lookup", "environment", "category", "updated_at"),
    )


# ==========================================================================
# 账本与资金流（LED-01 / LED-04）
# ==========================================================================


class LedgerEvent(Base):
    """业务账本追加事件（LED-01）。

    纠错使用更正事件（``correction_of_id`` 指向被更正的事件），不原地改写。
    """

    __tablename__ = "ledger_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    correction_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_event.id", ondelete="SET NULL")
    )
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "dedupe_key", name="uq_ledger_dedupe"),
        Index("ix_ledger_time", "environment", "occurred_at"),
    )


class CashFlow(Base):
    """外部资金流（LED-04 / LED-03）。

    充值、提现、划转等外部变化计入账户总风险但不自动归属某策略。
    """

    __tablename__ = "cash_flow"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    external: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "dedupe_key", name="uq_cash_flow_dedupe"),
    )


class EquitySnapshot(Base):
    """风控权益序列（LED-04）。

    唯一、版本化的估值政策；交易所返回的 equity 仅用于核对，不再叠加期权价值。
    """

    __tablename__ = "equity_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    net_external_flow: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    adjusted_equity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    #: False = 估值不完整，禁止据此批准新增风险。
    valuation_complete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("environment", "at", name="uq_equity_at"),
        Index("ix_equity_lookup", "environment", "at"),
    )


class EquityBaseline(Base):
    """日初权益基线与回撤高点（LED-04）。

    重启或换日都不重置回撤高点；基线缺失且无法重建时必须暂停开仓。
    """

    __tablename__ = "equity_baseline"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    day: Mapped[str] = mapped_column(String(16), nullable=False)  # YYYY-MM-DD（本地日界线）
    equity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: False = 不完整交易日（首次接入），不得据此宣称零亏损。
    complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    drawdown_peak: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), default=Decimal("0"), nullable=False
    )
    established_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "day", name="uq_equity_baseline_day"),
    )


# ==========================================================================
# 对账（LED-02 / LED-03）
# ==========================================================================


class ReconciliationRun(Base):
    """对账运行记录（LED-02）。

    缺失成交、未知订单和预留差异修复完成前不恢复开仓。
    """

    __tablename__ = "reconciliation_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    watermark: Mapped[str | None] = mapped_column(String(128))
    cursor: Mapped[str | None] = mapped_column(Text)
    differences: Mapped[list[object]] = mapped_column(JSONB, default=list)
    repaired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allows_resume: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (Index("ix_recon_lookup", "environment", "started_at"),)


# ==========================================================================
# 控制命令与告警（OPS-02 / OPS-04）
# ==========================================================================


class ControlCommand(Base):
    """人工控制命令（OPS-02）。

    命令有唯一编号、持久化状态及审计；重复点击不重复生成执行意图。
    """

    __tablename__ = "control_command"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    command_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("environment", "command_id", name="uq_control_command_id"),
    )


class Alert(Base):
    """告警（OPS-04）。

    告警去重、保留首发与恢复事件；通知失败落库并重试。
    """

    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    alert_key: Mapped[str] = mapped_column(String(256), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ACTIVE/DELIVERED/RESOLVED
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_delivery_error: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("environment", "alert_key", name="uq_alert_key"),
        Index("ix_alert_status", "environment", "status"),
    )


class ExecutorLease(Base):
    """执行者租约审计（EXE-05）。

    实际互斥用 Postgres advisory lock；本表只记录持有者与心跳，用于审计。
    """

    __tablename__ = "executor_lease"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    account_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    holder_id: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_lease_lookup", "environment", "account_uid", "released_at"),
    )


class RuntimeState(Base):
    """服务心跳与 paper 原子检查点；不保存密钥（OPS-01 / STR-05）。"""

    __tablename__ = "runtime_state"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "key", name="uq_runtime_state"),
    )


__all__ = [name for name in dir() if name[0].isupper()]
