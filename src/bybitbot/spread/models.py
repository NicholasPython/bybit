"""双腿价差套利的持久化模型（PRD §5 数据模型概要）。

四张表：

* ``spread_pair_config`` —— 激活的套利币对配置（协整系数、Z-Score 阈值、资金上限）；
* ``spread_pair_trade`` —— 每一次完整套利生命周期的主表；
* ``spread_leg_order`` —— 腿订单明细（关联 ``pair_trade_id`` 与 Bybit 订单标识）；
* ``spread_reconciliation_log`` —— 准实时对账快照与偏差处理记录。

边界说明（重要）
----------------
本子系统的物理订单**不进入**既有的 ``order`` 表。原因是既有 ``order``
表以 ``trade_intent`` 为主键外键，而套利腿的下单单位是「一次配对」而不是
「一次意图」：两条腿共享同一个 ``pair_trade_id``，按配对占用资金与杠杆，
若拆成两个意图会让预留与限额各算一半，既有的逐意图风控口径会与
配对级口径打架。

由此带来的已知边界：套利腿走 ``category=linear``，而既有对账只覆盖
``spot`` / ``option``，因此**线性腿的对账由本子系统的
``spread_reconciliation_log`` 负责**。在配对子系统接入实盘之前，
必须先解决 UTA 账户下保证金变动与既有现金对账的冲突
（见差距评估 §4.3「保证金占用风控」与 §5.2）。
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
from sqlalchemy.orm import Mapped, mapped_column

from bybitbot.db.models import Base, utcnow


def _now() -> dt.datetime:
    return utcnow()


class SpreadPairConfig(Base):
    """套利币对配置（PRD §5 ``arbitrage_pairs_config``）。"""

    __tablename__ = "spread_pair_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 稳定的配对标识，例如 ``BTCUSDT:BTC-26MAR27``。
    pair_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 被动腿（流动性较差的一腿）。
    symbol_a: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 主动对冲腿。
    symbol_b: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 协整对冲系数 β。
    hedge_ratio: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    #: 入场偏离度阈值 |Z| ≥ z_entry。
    z_entry: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    #: 收敛了结阈值 |Z| ≤ z_exit。
    z_exit: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    #: 硬止损阈值 |Z| ≥ z_stop。
    z_stop: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    #: 单组最大名义资金。
    max_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    #: 单腿裸敞口名义上限。
    max_unhedged_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    #: 被动腿挂单超时（毫秒）。
    passive_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    #: 追单补偿窗口（毫秒）。
    recovery_window_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=5000)
    #: 追单允许的最大滑点（基点）。
    max_slippage_bps: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=50)
    #: ``ACTIVE`` / ``PAUSED`` / ``DISABLED``。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DISABLED")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "pair_key", name="uq_spread_pair_config"),
        CheckConstraint(
            "status in ('ACTIVE', 'PAUSED', 'DISABLED')", name="ck_spread_pair_config_status"
        ),
        CheckConstraint("hedge_ratio > 0", name="ck_spread_pair_config_ratio"),
        CheckConstraint("z_exit < z_entry AND z_entry < z_stop", name="ck_spread_pair_config_z"),
        CheckConstraint("max_notional > 0", name="ck_spread_pair_config_notional"),
        CheckConstraint(
            "max_unhedged_notional >= 0 AND max_unhedged_notional < max_notional",
            name="ck_spread_pair_config_unhedged",
        ),
        Index("ix_spread_pair_config_status", "environment", "status"),
    )


class SpreadPairTrade(Base):
    """一次完整套利生命周期的主表（PRD §5 ``pair_trades``）。"""

    __tablename__ = "spread_pair_trade"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 全局唯一的配对单标识，同时是订单编号的组成部分。
    pair_trade_id: Mapped[str] = mapped_column(String(32), nullable=False)
    pair_key: Mapped[str] = mapped_column(String(64), nullable=False)
    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("spread_pair_config.id", ondelete="SET NULL")
    )
    #: ``OPENING`` / ``HEDGED`` / ``CLOSING`` / ``CLOSED`` / ``ABORTED`` /
    #: ``ERROR_UNHEDGED``。
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="OPENING")
    #: ``SHORT_A_LONG_B`` 或 ``LONG_A_SHORT_B``。
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol_a: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol_b: Mapped[str] = mapped_column(String(64), nullable=False)
    side_a: Mapped[str] = mapped_column(String(8), nullable=False)
    side_b: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 冻结的信号、约束与恢复计时器；重启不得使用新配置改写在途订单。
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    account_uid: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 开仓时的目标名义价值。
    target_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    filled_qty_a: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    filled_qty_b: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    avg_price_a: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    avg_price_b: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    notional_a: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    notional_b: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    #: 开仓时的预期总利润，用于资金费率逆风保护（PRD FR-03.4）。
    expected_profit: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    #: 价差盈亏。
    spread_pnl: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    #: 资金费率净收支（正为收入）。
    funding_pnl: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    #: 交易手续费合计。
    trading_fee: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    #: 净收益 = 价差盈亏 + 资金费率净收支 − 手续费。
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal("0"))
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hedged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "pair_trade_id", name="uq_spread_pair_trade"),
        CheckConstraint(
            "state in ('OPENING', 'HEDGED', 'CLOSING', 'CLOSED', 'ABORTED', 'ERROR_UNHEDGED')",
            name="ck_spread_pair_trade_state",
        ),
        CheckConstraint(
            "direction in ('SHORT_A_LONG_B', 'LONG_A_SHORT_B')",
            name="ck_spread_pair_trade_direction",
        ),
        CheckConstraint("filled_qty_a >= 0 AND filled_qty_b >= 0", name="ck_spread_pair_trade_qty"),
        CheckConstraint("target_notional > 0", name="ck_spread_pair_trade_notional"),
        Index("ix_spread_pair_trade_state", "environment", "state"),
        Index("ix_spread_pair_trade_pair_key", "environment", "pair_key", "created_at"),
    )


class SpreadLegOrder(Base):
    """腿订单明细（PRD §5 ``leg_orders``）。

    物理事实（数量、成交均价、状态）以交易所为准，本表是腿与配对之间的
    归属关系与审计线索。
    """

    __tablename__ = "spread_leg_order"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    pair_trade_id: Mapped[int] = mapped_column(
        ForeignKey("spread_pair_trade.id", ondelete="CASCADE"), nullable=False
    )
    #: ``A`` 或 ``B``。
    leg: Mapped[str] = mapped_column(String(1), nullable=False)
    #: ``PASSIVE`` / ``ACTIVE`` / ``RECOVERY`` / ``FLATTEN``。
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    order_link_id: Mapped[str] = mapped_column(String(36), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="Limit")
    time_in_force: Mapped[str] = mapped_column(String(16), nullable=False, default="GTC")
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    qty_filled: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    #: 与既有订单状态机同一套可信度口径。
    sync_state: Mapped[str] = mapped_column(String(16), nullable=False, default="NONE")
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("environment", "order_link_id", name="uq_spread_leg_order_link"),
        CheckConstraint("leg in ('A', 'B')", name="ck_spread_leg_order_leg"),
        CheckConstraint(
            "role in ('PASSIVE', 'ACTIVE', 'RECOVERY', 'FLATTEN')",
            name="ck_spread_leg_order_role",
        ),
        CheckConstraint(
            "qty > 0 AND qty_filled >= 0 AND qty_filled <= qty",
            name="ck_spread_leg_qty",
        ),
        CheckConstraint(
            "status in ('PENDING', 'WORKING', 'PARTIAL', 'FILLED', "
            "'CANCELED', 'REJECTED', 'UNKNOWN')",
            name="ck_spread_leg_order_status",
        ),
        CheckConstraint(
            "sync_state in ('NONE', 'PENDING', 'CONFIRMED', 'UNCERTAIN')",
            name="ck_spread_leg_order_sync",
        ),
        Index("ix_spread_leg_order_pair", "pair_trade_id", "leg"),
        Index("ix_spread_leg_order_link", "environment", "order_link_id"),
    )


class SpreadExecution(Base):
    """不可变的逐笔线性成交事实；订单累计快照不能替代此表。"""

    __tablename__ = "spread_execution"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("spread_leg_order.id"), nullable=False)
    exec_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exec_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    exec_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    exec_fee: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    exec_fee_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    exec_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("environment", "exec_id", name="uq_spread_execution"),
        CheckConstraint("exec_qty > 0 AND exec_price > 0", name="ck_spread_execution_qty"),
        Index("ix_spread_execution_order", "order_id"),
    )


class SpreadReconciliationLog(Base):
    """准实时对账快照与偏差处理（PRD §5 ``reconciliation_logs``）。"""

    __tablename__ = "spread_reconciliation_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 对账针对的配对；账户级快照为 ``NULL``。
    pair_trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("spread_pair_trade.id", ondelete="CASCADE")
    )
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_qty_a: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    local_qty_b: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    exchange_qty_a: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    exchange_qty_b: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    local_notional_a: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    local_notional_b: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    exchange_notional_a: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    exchange_notional_b: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    #: 数量差（本地减交易所）的绝对值。
    difference: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("0")
    )
    #: ``CLEAN`` / ``DIFFERENCE`` / ``REPAIRED``。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="CLEAN")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 已执行的修复动作（JSON 数组文本）。
    actions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('CLEAN', 'DIFFERENCE', 'REPAIRED')",
            name="ck_spread_reconciliation_status",
        ),
        CheckConstraint("difference >= 0", name="ck_spread_reconciliation_difference"),
        Index("ix_spread_reconciliation_checked", "environment", "checked_at"),
        Index("ix_spread_reconciliation_pair", "pair_trade_id", "checked_at"),
    )


__all__ = [
    "SpreadExecution",
    "SpreadLegOrder",
    "SpreadPairConfig",
    "SpreadPairTrade",
    "SpreadReconciliationLog",
]
