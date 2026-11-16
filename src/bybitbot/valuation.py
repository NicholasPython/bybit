"""净值与估值政策（LED-04）。

需求原文：
    风控 ``Equity`` 使用唯一、版本化的估值政策：自有净现金按指定现货/汇率源换算，
    加各期权多头按指定标记价及**已验证报价单位**计算的价值，并按字段语义扣除
    尚未计入净现金的负债。日初权益、日损益和历史高点全部使用同一政策。
    交易所已包含期权价值的 ``equity/totalEquity`` 仅用于核对，不再叠加期权价值。
    费用已反映在权益中时不再次扣除。

    当日资金流调整损益：
    ``PnL_day(t) = Equity(t) - Equity(day_start) - NetExternalFlow(day_start,t)``。
    从已确认起点计算资金流调整权益
    ``AdjustedEquity(t) = Equity(t) - CumulativeNetExternalFlow(t)``，
    回撤金额为该序列历史高点减当前值。

    数据不完整时展示「估值不可用」，不能显示为零风险或用旧净值解除熔断。

关键设计
--------
    * 估值政策带版本号（``policy_version``），历史序列不可混用不同版本；
      政策变更只在暂停状态下执行，并保留历史（§4.8 末段）。
    * 估值不完整 → ``valuation_complete=False`` → 禁止据此批准新增风险。
    * 交易所自报的 equity **只用于核对**，不参与权益计算，避免与本地
      现金+期权价值重复计算。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from bybitbot.adapters.base import AccountView, PositionView
from bybitbot.clock import local_day, local_day_start_utc
from bybitbot.money import dec
from bybitbot.terms.valuation import ValuationPolicy, option_value


@dataclass
class ValuationInput:
    """一次估值需要的输入。"""

    account: AccountView
    #: 现金币种 → 估值币种的换算率。
    cash_rates: dict[str, Decimal]
    #: 期权多头持仓（含标记价）。
    option_positions: list[PositionView] = field(default_factory=list)
    #: 期权结算币种 → 估值币种的换算率。
    settle_rates: dict[str, Decimal] = field(default_factory=dict)
    #: 期权合约乘数（每张对应多少标的）。
    contract_multiplier: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class Valuation:
    """一次估值的结果。"""

    equity: Decimal
    currency: str
    policy_version: str
    complete: bool
    #: 不完整的原因；``complete=False`` 时必须展示而不是当作零风险。
    incomplete_reason: str = ""
    details: dict[str, object] = field(default_factory=dict)


def value_account(policy: ValuationPolicy, data: ValuationInput) -> Valuation:
    """按政策计算账户权益。

    口径：
        Equity = Σ(自有净现金 × 汇率) + Σ(期权多头价值 × 汇率) - 尚未计入净现金的负债

    净现金来自 wallet_balance，不把交易所自报 equity 再叠加进来。
    存在负债或期权空头时，当前仅多头政策不能完整估值，必须拒绝作为风控输入。
    """
    if data.account.has_debt:
        return Valuation(
            equity=Decimal("0"),
            currency=policy.currency,
            policy_version=policy.version,
            complete=False,
            incomplete_reason="账户存在负债，净现金语义须对账后才能估值",
        )
    total = Decimal("0")
    details: dict[str, object] = {"cash": {}, "options": {}, "liabilities": {}}
    missing_rate: list[str] = []

    for coin, balance in data.account.coins.items():
        own_cash = balance.wallet_balance
        if own_cash is None:
            missing_rate.append(f"{coin}:wallet_balance")
            continue
        rate = data.cash_rates.get(coin.upper())
        if rate is None:
            if own_cash != 0:
                missing_rate.append(coin.upper())
            continue
        value = dec(own_cash) * dec(rate)
        total += value
        details["cash"][coin.upper()] = str(value)

        # 负债：按字段语义扣除（尚未计入净现金的部分）。
        liability = dec(balance.borrowed) + dec(balance.accrued_interest)
        if liability != 0:
            liability_value = liability * dec(rate)
            total -= liability_value
            details["liabilities"][coin.upper()] = str(liability_value)

    option_total = Decimal("0")
    for position in data.option_positions:
        size = dec(position.size)
        if size < 0 or (size > 0 and position.side == "Sell"):
            missing_rate.append(f"{position.symbol}:不支持期权空头估值")
            continue
        if size == 0:
            continue
        price = getattr(position, policy.option_price_field, None)
        if price is None:
            price = position.mark_price
        if price is None:
            missing_rate.append(f"{position.symbol}:price")
            continue
        settle = (position.settle_coin or policy.currency).upper()
        rate = data.settle_rates.get(settle)
        if rate is None:
            missing_rate.append(settle)
            continue
        multiplier = data.contract_multiplier.get(position.symbol, Decimal("1"))
        try:
            value = option_value(
                price=dec(price), qty=size, multiplier=dec(multiplier), rate=dec(rate)
            )
        except ValueError:
            missing_rate.append(f"{position.symbol}:无效价格、乘数或汇率")
            continue
        option_total += value
        details["options"][position.symbol] = str(value)
    total += option_total

    if missing_rate:
        return Valuation(
            equity=total,
            currency=policy.currency,
            policy_version=policy.version,
            complete=False,
            incomplete_reason=("缺少估值所需的汇率或价格：" + ", ".join(sorted(set(missing_rate)))),
            details=details,
        )

    return Valuation(
        equity=total,
        currency=policy.currency,
        policy_version=policy.version,
        complete=True,
        details=details,
    )


def day_pnl(
    *,
    equity_now: Decimal,
    equity_day_start: Decimal,
    net_external_flow: Decimal,
) -> Decimal:
    """当日资金流调整损益。

    ``PnL_day(t) = Equity(t) - Equity(day_start) - NetExternalFlow(day_start,t)``

    净流入为正、净流出为负，按发生时的估值换算（LED-04）。
    """
    return dec(equity_now) - dec(equity_day_start) - dec(net_external_flow)


def adjusted_equity(*, equity: Decimal, cumulative_net_external_flow: Decimal) -> Decimal:
    """资金流调整权益。

    ``AdjustedEquity(t) = Equity(t) - CumulativeNetExternalFlow(t)``
    """
    return dec(equity) - dec(cumulative_net_external_flow)


def drawdown_amount(*, current_adjusted_equity: Decimal, peak: Decimal) -> Decimal:
    """回撤金额 = 调整后权益的历史高点 - 当前值。"""
    return max(Decimal("0"), dec(peak) - dec(current_adjusted_equity))


def day_key(moment: dt.datetime, tz: str) -> str:
    """日界线键（本地日）。日界线切换只更新日统计，不解除已触发的暂停。"""
    return local_day(moment, tz)


def day_start(moment: dt.datetime, tz: str) -> dt.datetime:
    """日初时刻（UTC）。"""
    return local_day_start_utc(moment, tz)


__all__ = [
    "Valuation",
    "ValuationInput",
    "ValuationPolicy",
    "adjusted_equity",
    "day_key",
    "day_pnl",
    "day_start",
    "drawdown_amount",
    "value_account",
]
