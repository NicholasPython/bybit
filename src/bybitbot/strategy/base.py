"""策略接口与交易意图生成（STR-01）。

需求原文：
    策略仅生成标准交易意图：策略与配置版本、唯一信号编号、账户/环境、
    产品与合约、开平仓目的、方向、数量及其单位、价格边界、有效期、
    退出规则引用。相同输入与版本产生可解释的确定性结果。

关键约束：
    * 策略**不能**直接使用交易密钥或调用交易写接口（§3）——
      因此策略只产出 :class:`IntentDraft`，不接触任何适配器。
    * 信号编号必须确定：同一计划时刻、同一规则、同一策略版本
      得到同一个编号（STR-02 的去重基础）。
    * 数量必须带单位（``qty_unit``）—— 现货与期权的数量含义不同，
      不可隐式复用（SPT-01）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from bybitbot.db.models import TradeIntent
from bybitbot.money import dec


@dataclass(frozen=True)
class SignalContext:
    """一次信号评估的上下文。"""

    environment: str
    account_uid: str
    strategy_id: int
    strategy_version: int
    rule_id: str
    #: 计划时刻（UTC）。同一次计划的重试必须传入同一个值。
    planned_at: dt.datetime
    #: 决策使用的数据快照引用（MKT-03 / STR-01）。
    quote_snapshot_id: int | None = None


@dataclass(frozen=True)
class IntentDraft:
    """策略产出的交易意图草稿（尚未落库）。"""

    action: str
    category: str
    symbol: str
    side: str
    qty: Decimal
    qty_unit: str
    currency: str
    spend_limit: Decimal
    price_limit: Decimal | None = None
    reduce_qty: Decimal | None = None
    underlying: str | None = None
    delivery_time: dt.datetime | None = None
    strike_price: Decimal | None = None
    options_type: str | None = None
    exit_rules: tuple[dict[str, object], ...] = ()
    #: 意图有效期。过期后不再产生新提交（STR-02）。
    ttl_seconds: int = 300
    #: 决策理由，写入风控决策与账本，供人工追溯（STR-01「可解释」）。
    rationale: dict[str, object] = field(default_factory=dict)
    validation_position_id: int | None = None
    coverage_symbol: str | None = None


@runtime_checkable
class Strategy(Protocol):
    """策略协议。

    实现必须满足：
        * **确定性** —— 相同输入与配置版本产生相同输出（STR-01）；
        * **只读** —— 不修改账户、不调用交易写接口；
        * **可跳过** —— 没有符合条件的合约时返回空列表，而不是报错。
    """

    #: 策略种类标识，对应 ``Strategy.kind``。
    kind: str
    #: 策略实例名。
    name: str

    def evaluate(
        self, ctx: SignalContext, deps: StrategyDeps
    ) -> list[IntentDraft]:
        """评估一次信号，返回交易意图草稿列表（可为空）。"""
        ...


@runtime_checkable
class StrategyDeps(Protocol):
    """策略可以看到的全部只读依赖。

    刻意保持最小：策略看不到密钥、看不到写接口，只能看到市场事实与
    自身配置。
    """

    @property
    def now(self) -> dt.datetime: ...

    def instrument(self, category: str, symbol: str): ...

    def instruments(self, category: str) -> list: ...

    def quote(self, category: str, symbol: str): ...

    def config(self) -> dict[str, object]: ...


def signal_id(ctx: SignalContext) -> str:
    """生成唯一信号编号（STR-01 / STR-02）。

    组成：策略实例 + 规则编号 + 计划时刻 + 配置版本。
    配置版本进编号，是为了让「同一计划时刻、换了一版配置」成为两个不同的
    意图，而不是把旧意图悄悄改写（STR-04：配置变更生成不可变版本）。
    """
    return (
        f"{ctx.strategy_id}:{ctx.strategy_version}:{ctx.rule_id}:"
        f"{ctx.planned_at.astimezone(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )


def build_intent(
    ctx: SignalContext, draft: IntentDraft
) -> TradeIntent:
    """把草稿转成可落库的 :class:`TradeIntent`。

    尚未落库 —— 调用方负责在同一事务里做去重（``uq_intent_signal``）。
    """
    now = dt.datetime.now(dt.UTC)
    expires_at = ctx.planned_at + dt.timedelta(seconds=draft.ttl_seconds)
    return TradeIntent(
        environment=ctx.environment,
        account_uid=ctx.account_uid,
        strategy_id=ctx.strategy_id,
        strategy_version=ctx.strategy_version,
        signal_id=signal_id(ctx),
        action=draft.action,
        category=draft.category,
        symbol=draft.symbol,
        side=draft.side.lower(),
        qty=dec(draft.qty),
        qty_unit=draft.qty_unit,
        price_limit=dec(draft.price_limit) if draft.price_limit is not None else None,
        spend_limit=dec(draft.spend_limit),
        currency=draft.currency.upper(),
        reduce_qty=dec(draft.reduce_qty) if draft.reduce_qty is not None else None,
        underlying=draft.underlying,
        delivery_time=draft.delivery_time,
        strike_price=dec(draft.strike_price) if draft.strike_price is not None else None,
        options_type=draft.options_type,
        validation_position_id=draft.validation_position_id,
        coverage_symbol=draft.coverage_symbol,
        exit_rules=list(draft.exit_rules),
        quote_snapshot_id=ctx.quote_snapshot_id,
        expires_at=expires_at,
        status="PREPARED",
        created_at=now,
    )


__all__ = [
    "IntentDraft",
    "SignalContext",
    "Strategy",
    "StrategyDeps",
    "build_intent",
    "signal_id",
]
