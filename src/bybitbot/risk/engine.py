"""独立风控引擎（RSK-01 / RSK-03 / RSK-04 / RSK-05）。

需求原文要点：
    * RSK-01：统一检查白名单、单笔额度、按策略及账户的现货敞口、期权权利金
      预算、最大订单数量、费用余量、价格偏离、价差、信号时效及行情/账户状态。
      每次检查输出批准或拒绝、原因和使用的配置版本。
    * RSK-03：触发日亏损/回撤上限、行情或账户失效、未知订单、严重对账差异、
      执行权丢失等事件时暂停新增风险。
    * RSK-04：减仓路径与开仓路径**分别判定**。达到开仓预算/亏损阈值不应
      单独阻止可信的减仓，但减仓仍必须满足身份、执行权、可信持仓、数量预留、
      价格边界和审计条件。

风控引擎是纯函数式的判定层：不写库、不调交易所，只给出结论。
资金与持仓的实际预留由 :mod:`bybitbot.risk.reservation` 在事务里完成。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bybitbot.adapters.base import AccountView, InstrumentSpec, PositionView, Ticker
from bybitbot.clock import now_utc
from bybitbot.db.models import Order, TradeIntent
from bybitbot.money import dec, pct
from bybitbot.risk.delta import (
    DeltaCandidate,
    DeltaUnits,
    evaluate_delta_bounds,
    spot_delta_change,
)
from bybitbot.settings.loader import AppConfig
from bybitbot.settings.models import IntentAction


@dataclass
class RiskCheckResult:
    """单项检查结果。"""

    code: str
    approved: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "approved": self.approved, "detail": self.detail}


@dataclass
class RiskEvaluation:
    """整体风控结论。"""

    approved: bool
    reason_code: str
    reason_detail: str
    checks: list[RiskCheckResult] = field(default_factory=list)
    config_version: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "config_version": self.config_version,
            "checks": [c.as_dict() for c in self.checks],
        }

    @property
    def rejected_checks(self) -> list[RiskCheckResult]:
        return [c for c in self.checks if not c.approved]


@dataclass
class OpenOrderExposure:
    """未完成订单带来的敞口与 Delta 变化。"""

    order_id: int
    symbol: str
    side: str
    leaves_qty: Decimal
    delta_if_filled: Decimal | None
    order_link_id: str = ""


@dataclass(frozen=True)
class ValidationLimits:
    """验证价差协议口径的限额。

    两腿价差的风险口径由**协议自身**给出：首腿支出上限、入场点差比例与
    cohort 风险预算（见 :mod:`bybitbot.validation.protocol`）。这些口径比
    产品的单腿限额更贴合两腿结构——期权的单腿买卖点差本来就远大于产品
    面向现货设定的点差上限，首腿支出也按协议上限而非单笔上限衡量。

    带验证持仓的意图按协议口径判定；其余策略仍用产品限额。
    """

    #: 单腿支出上限（协议首腿支出上限，USDT）。
    max_spend_per_trade: Decimal
    #: 入场点差比例上限（协议口径，小数）。
    max_spread_fraction: Decimal
    #: 权利金集中度上限（协议首腿支出上限，USDT）。
    premium_cap_per_underlying: Decimal


@dataclass
class RiskContext:
    """风控判定所需的全部输入。"""

    account: AccountView
    positions: list[PositionView]
    quotes: dict[str, Ticker]
    open_orders: list[OpenOrderExposure]
    #: 每个标的当前的策略归属现货敞口（估值币种）。
    strategy_spot_exposure: Decimal = Decimal("0")
    #: 账户级现货敞口（估值币种）。
    account_spot_exposure: Decimal = Decimal("0")
    #: 账户级期权权利金预算占用。
    account_premium_used: Decimal = Decimal("0")
    #: 风控权益（估值币种）；比例类限额以此为分母。
    risk_equity: Decimal = Decimal("0")
    #: 各标的期权权利金占用。
    premium_used_per_underlying: dict[str, Decimal] = field(default_factory=dict)
    #: 各到期日的权利金占用。
    premium_used_per_expiry: dict[str, Decimal] = field(default_factory=dict)
    #: 当前未完成挂单数量。
    open_order_count: int = 0
    #: 日内已实现 + 未实现亏损（估值币种，正数表示亏损）。
    daily_pnl: Decimal = Decimal("0")
    #: 相对历史高点的回撤金额（估值币种，正数表示回撤）。
    drawdown: Decimal = Decimal("0")
    #: 账户级暂停原因（RSK-03）。
    account_pause_reasons: list[str] = field(default_factory=list)
    #: 存在结果未知的订单（EXE-02）。
    unknown_order_count: int = 0
    #: 是否处于可执行状态（执行权是否在手，EXE-05）。
    execution_authority: bool = True
    #: 行情整体是否可信（MKT-03）。
    market_trusted: bool = True
    #: 账户快照是否完整可信（MKT-03 / LED-02）。
    account_trusted: bool = True
    #: Greeks 单位映射。
    delta_units: DeltaUnits = field(default_factory=DeltaUnits)
    #: 验证价差协议口径的限额；``None`` 表示按产品限额判定。
    validation_limits: ValidationLimits | None = None
    #: 本次判定使用的配置版本标识。
    config_version: str = ""
    valuation_trusted: bool = False
    identity_trusted: bool = False
    account_delta_per_underlying: dict[str, Decimal] | None = None
    instruments: dict[str, InstrumentSpec] = field(default_factory=dict)


class RiskEngine:
    """独立风控。

    构造时固定配置；每次判定传入一个 :class:`RiskContext`。
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._limits = config.risk
        self._budget = config.budget
        self._market = config.market
        self._option = config.option

    def config_version(self) -> str:
        safe = self.config.model_dump_json(exclude={"environments"})
        return hashlib.sha256(safe.encode()).hexdigest()[:20]

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def evaluate(self, intent: TradeIntent, ctx: RiskContext) -> RiskEvaluation:
        """按开仓/减仓路径分别判定（RSK-04）。"""
        if intent.action in (
            IntentAction.SPOT_SELL,
            IntentAction.OPTION_SELL_CLOSE,
            IntentAction.OPTION_BUY_CLOSE,
        ):
            return self._evaluate_reduce(intent, ctx)
        return self._evaluate_open(intent, ctx)

    # ------------------------------------------------------------------
    # 开仓路径
    # ------------------------------------------------------------------

    def _evaluate_open(self, intent: TradeIntent, ctx: RiskContext) -> RiskEvaluation:
        checks: list[RiskCheckResult] = []
        is_option = intent.action in {IntentAction.OPTION_BUY_OPEN, IntentAction.OPTION_SELL_OPEN}

        checks.append(self._check_account_pause(ctx))
        checks.extend(
            [
                RiskCheckResult(
                    "EXECUTION_AUTHORITY",
                    ctx.execution_authority,
                    "" if ctx.execution_authority else "执行资格无效",
                ),
                RiskCheckResult(
                    "IDENTITY_TRUSTED",
                    ctx.identity_trusted,
                    "" if ctx.identity_trusted else "账户身份未经确认",
                ),
                RiskCheckResult(
                    "VALUATION_TRUSTED",
                    ctx.valuation_trusted,
                    "" if ctx.valuation_trusted else "缺少可信估值或风险基线",
                ),
                RiskCheckResult(
                    "UNKNOWN_ORDERS",
                    ctx.unknown_order_count == 0,
                    "" if ctx.unknown_order_count == 0 else "账户存在未知订单",
                ),
                RiskCheckResult(
                    "ACCOUNT_DEBT",
                    not ctx.account.has_debt,
                    "" if not ctx.account.has_debt else "账户存在负债",
                ),
            ]
        )
        checks.append(self._check_signal_freshness(intent))
        checks.append(self._check_market_state(ctx))
        checks.append(self._check_per_trade(intent, ctx))
        checks.append(self._check_price_deviation(intent, ctx))
        checks.append(self._check_spread(intent, ctx))
        checks.append(self._check_fee_buffer(intent))
        checks.append(self._check_max_open_orders(ctx))
        checks.append(self._check_risk_limits(ctx))

        if is_option:
            checks.append(self._check_option_premium_budget(intent, ctx))
            checks.append(self._check_premium_equity_ratio(intent, ctx))
            checks.append(self._check_option_window(intent))
            checks.append(self._check_option_expiry(intent))
            checks.append(self._check_premium_concentration(intent, ctx))
        else:
            checks.append(self._check_spot_exposure(intent, ctx))

        delta_check = self._check_delta(intent, ctx)
        checks.append(
            RiskCheckResult(
                code="DELTA_BOUNDS",
                approved=delta_check.approved,
                detail=delta_check.detail or delta_check.reason_code,
            )
        )

        rejected = [c for c in checks if not c.approved]
        if rejected:
            first = rejected[0]
            return RiskEvaluation(
                approved=False,
                reason_code=first.code,
                reason_detail=first.detail,
                checks=checks,
                config_version=ctx.config_version or self.config_version(),
            )
        return RiskEvaluation(
            approved=True,
            reason_code="APPROVED",
            reason_detail="",
            checks=checks,
            config_version=ctx.config_version or self.config_version(),
        )

    # ------------------------------------------------------------------
    # 减仓路径（RSK-04）
    # ------------------------------------------------------------------

    def _evaluate_reduce(self, intent: TradeIntent, ctx: RiskContext) -> RiskEvaluation:
        """减仓不走开仓限额，但身份、执行权、可信持仓、数量预留、
        价格边界与审计条件仍必须满足。"""
        checks: list[RiskCheckResult] = []

        checks.append(
            RiskCheckResult(
                code="EXECUTION_AUTHORITY",
                approved=ctx.execution_authority,
                detail="" if ctx.execution_authority else "执行权已失效，不得发出新的买卖单",
            )
        )
        checks.append(
            RiskCheckResult(
                code="ACCOUNT_TRUSTED",
                approved=ctx.account_trusted,
                detail="" if ctx.account_trusted else "账户快照不可信，暂停一切新单",
            )
        )
        checks.append(
            RiskCheckResult(
                code="IDENTITY_TRUSTED",
                approved=ctx.identity_trusted,
                detail="" if ctx.identity_trusted else "账户身份未经确认",
            )
        )
        checks.append(self._check_signal_freshness(intent))
        checks.append(self._check_price_deviation(intent, ctx))
        checks.append(
            RiskCheckResult(
                code="UNKNOWN_ORDERS",
                approved=ctx.unknown_order_count == 0,
                detail=(
                    ""
                    if ctx.unknown_order_count == 0
                    else f"存在 {ctx.unknown_order_count} 个结果未知的订单，账户必须暂停"
                ),
            )
        )

        rejected = [c for c in checks if not c.approved]
        if rejected:
            first = rejected[0]
            return RiskEvaluation(
                approved=False,
                reason_code=first.code,
                reason_detail=first.detail,
                checks=checks,
                config_version=ctx.config_version or self.config_version(),
            )
        return RiskEvaluation(
            approved=True,
            reason_code="APPROVED",
            reason_detail="",
            checks=checks,
            config_version=ctx.config_version or self.config_version(),
        )

    # ------------------------------------------------------------------
    # 单项检查
    # ------------------------------------------------------------------

    def _check_account_pause(self, ctx: RiskContext) -> RiskCheckResult:
        if ctx.account_pause_reasons:
            return RiskCheckResult(
                code="ACCOUNT_PAUSED",
                approved=False,
                detail="账户处于暂停状态：" + "；".join(ctx.account_pause_reasons),
            )
        return RiskCheckResult(code="ACCOUNT_PAUSED", approved=True)

    def _check_signal_freshness(self, intent: TradeIntent) -> RiskCheckResult:
        expires = intent.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.UTC)
        expired = now_utc() >= expires
        return RiskCheckResult(
            code="SIGNAL_FRESHNESS",
            approved=not expired,
            detail="" if not expired else "信号已过期，拒绝提交",
        )

    def _check_market_state(self, ctx: RiskContext) -> RiskCheckResult:
        if not ctx.market_trusted:
            return RiskCheckResult(
                code="MARKET_UNTRUSTED",
                approved=False,
                detail="行情状态不可信（断档未重建或快照过期）",
            )
        if not ctx.account_trusted:
            return RiskCheckResult(
                code="ACCOUNT_UNTRUSTED", approved=False, detail="账户状态不可信"
            )
        return RiskCheckResult(code="MARKET_STATE", approved=True)

    def _check_per_trade(self, intent: TradeIntent, ctx: RiskContext) -> RiskCheckResult:
        limits = ctx.validation_limits
        cap = (
            limits.max_spend_per_trade
            if limits is not None
            else self._budget.max_spend_per_trade.value
        )
        ok = intent.spend_limit <= cap
        return RiskCheckResult(
            code="PER_TRADE_LIMIT",
            approved=ok,
            detail="" if ok else f"单笔支出 {intent.spend_limit} 超过上限 {cap}",
        )

    def _check_price_deviation(self, intent: TradeIntent, ctx: RiskContext) -> RiskCheckResult:
        ticker = ctx.quotes.get(intent.symbol)
        if intent.price_limit is None or ticker is None:
            return RiskCheckResult("PRICE_DEVIATION", False, "缺少明确价格边界或双边报价")
        if ticker.is_crossed:
            return RiskCheckResult("PRICE_DEVIATION", False, "交叉报价，价格偏离无法判定")
        reference = ticker.ask_price if intent.side.lower() == "buy" else ticker.bid_price
        if (
            reference is None
            or not reference.is_finite()
            or reference <= 0
            or not intent.price_limit.is_finite()
            or intent.price_limit <= 0
        ):
            return RiskCheckResult("PRICE_DEVIATION", False, "缺少有效可成交参考价")
        moment = now_utc()
        if any(
            stamp is None
            or (moment - stamp).total_seconds() > self._market.max_quote_age_seconds
            or (stamp - moment).total_seconds() > 5
            for stamp in (ticker.source_ts, ticker.received_at)
        ):
            return RiskCheckResult("PRICE_DEVIATION", False, "参考报价已过期或时间无效")
        deviation = abs(intent.price_limit - reference) / reference
        limit = pct(self._market.max_price_deviation_bps)
        ok = deviation <= limit
        return RiskCheckResult(
            code="PRICE_DEVIATION",
            approved=ok,
            detail=(
                ""
                if ok
                else f"价格偏离 {deviation} 超过边界 {self._market.max_price_deviation_bps}bps"
            ),
        )

    def _check_spread(self, intent: TradeIntent, ctx: RiskContext) -> RiskCheckResult:
        ticker = ctx.quotes.get(intent.symbol)
        if ticker is None or ticker.bid_price is None or ticker.ask_price is None:
            return RiskCheckResult(
                code="SPREAD", approved=False, detail="缺少双边报价，无法判断价差"
            )
        if (
            not ticker.bid_price.is_finite()
            or not ticker.ask_price.is_finite()
            or ticker.bid_price <= 0
            or ticker.ask_price <= ticker.bid_price
        ):
            return RiskCheckResult(code="SPREAD", approved=False, detail="无效或交叉双边报价")
        fraction = (ticker.ask_price - ticker.bid_price) / ticker.bid_price
        limits = ctx.validation_limits
        if limits is not None:
            # 验证价差按协议的点差比例口径判定：产品面向现货的点差上限
            # 对期权两腿结构没有意义（期权点差天然更宽）。
            ok = fraction <= limits.max_spread_fraction
            return RiskCheckResult(
                code="SPREAD",
                approved=ok,
                detail=(
                    ""
                    if ok
                    else f"点差 {fraction:.4%} 超过协议上限 {limits.max_spread_fraction:.2%}"
                ),
            )
        spread_bps = fraction * dec(10000)
        ok = spread_bps <= self._market.max_spread_bps
        return RiskCheckResult(
            code="SPREAD",
            approved=ok,
            detail=(
                "" if ok else f"价差 {spread_bps:.1f}bps 超过上限 {self._market.max_spread_bps}bps"
            ),
        )

    def _check_fee_buffer(self, intent: TradeIntent) -> RiskCheckResult:
        total = intent.spend_limit + self._budget.fee_buffer.value
        ok = total <= self._budget.max_spend_per_trade.value + self._budget.fee_buffer.value
        return RiskCheckResult(
            code="FEE_BUFFER",
            approved=ok,
            detail="" if ok else "费用余量不足",
        )

    def _check_max_open_orders(self, ctx: RiskContext) -> RiskCheckResult:
        ok = ctx.open_order_count < self._budget.max_open_orders
        return RiskCheckResult(
            code="MAX_OPEN_ORDERS",
            approved=ok,
            detail=(
                ""
                if ok
                else f"未完成挂单数 {ctx.open_order_count} 已达上限 {self._budget.max_open_orders}"
            ),
        )

    def _check_risk_limits(self, ctx: RiskContext) -> RiskCheckResult:
        if ctx.daily_pnl >= self._limits.max_daily_loss.value:
            return RiskCheckResult(
                code="DAILY_LOSS_LIMIT",
                approved=False,
                detail=f"当日亏损 {ctx.daily_pnl} 达到上限 {self._limits.max_daily_loss.value}",
            )
        if ctx.drawdown >= self._limits.max_drawdown.value:
            return RiskCheckResult(
                code="DRAWDOWN_LIMIT",
                approved=False,
                detail=f"回撤 {ctx.drawdown} 达到上限 {self._limits.max_drawdown.value}",
            )
        return RiskCheckResult(code="RISK_LIMITS", approved=True)

    def _check_spot_exposure(self, intent: TradeIntent, ctx: RiskContext) -> RiskCheckResult:
        per_strategy_limit = self._budget.max_spot_exposure_per_strategy.value
        account_limit = self._budget.max_spot_exposure_account.value
        strategy_after = ctx.strategy_spot_exposure + intent.spend_limit
        account_after = ctx.account_spot_exposure + intent.spend_limit
        if strategy_after > per_strategy_limit:
            return RiskCheckResult(
                code="STRATEGY_EXPOSURE_LIMIT",
                approved=False,
                detail=(f"策略敞口将达到 {strategy_after}，超过上限 {per_strategy_limit}"),
            )
        if account_after > account_limit:
            return RiskCheckResult(
                code="ACCOUNT_EXPOSURE_LIMIT",
                approved=False,
                detail=f"账户敞口将达到 {account_after}，超过上限 {account_limit}",
            )
        return RiskCheckResult(code="SPOT_EXPOSURE", approved=True)

    def _check_option_premium_budget(
        self, intent: TradeIntent, ctx: RiskContext
    ) -> RiskCheckResult:
        budget = self._budget.option_premium_budget.value
        after = ctx.account_premium_used + intent.spend_limit
        if after > budget:
            return RiskCheckResult(
                code="PREMIUM_BUDGET",
                approved=False,
                detail=f"期权权利金将达到 {after}，超过预算 {budget}",
            )
        return RiskCheckResult(code="PREMIUM_BUDGET", approved=True)

    def _check_premium_equity_ratio(self, intent: TradeIntent, ctx: RiskContext) -> RiskCheckResult:
        """权利金总额占风控权益的比例上限。

        与绝对权利金预算相互独立：权益下降时按比例自动收紧，
        权益不可用时拒绝开仓（无法证明比例合规）。
        """
        ratio = self._limits.max_premium_to_equity_ratio
        if ratio is None:
            return RiskCheckResult(code="PREMIUM_EQUITY_RATIO", approved=True)
        equity = ctx.risk_equity
        if equity <= 0:
            return RiskCheckResult(
                code="PREMIUM_EQUITY_RATIO",
                approved=False,
                detail="风控权益不可用，无法校验权利金占权益比例",
            )
        after = ctx.account_premium_used + intent.spend_limit
        limit = equity * ratio
        if after > limit:
            return RiskCheckResult(
                code="PREMIUM_EQUITY_RATIO",
                approved=False,
                detail=f"期权权利金将达到 {after}，超过权益 {equity} 的 {ratio} 倍（{limit}）",
            )
        return RiskCheckResult(code="PREMIUM_EQUITY_RATIO", approved=True)

    def _check_option_window(self, intent: TradeIntent) -> RiskCheckResult:
        """禁开仓窗口（OPT-04）。"""
        now = now_utc()
        start = self._option.no_open_window_start_hour_utc
        end = self._option.no_open_window_end_hour_utc
        if start == end:
            return RiskCheckResult(code="NO_OPEN_WINDOW", approved=True)
        hour = now.hour
        if start < end:
            in_window = start <= hour < end
        else:  # 跨 UTC 零点
            in_window = hour >= start or hour < end
        return RiskCheckResult(
            code="NO_OPEN_WINDOW",
            approved=not in_window,
            detail=(
                ""
                if not in_window
                else f"当前处于禁开仓窗口 {start:02d}:00-{end:02d}:00 UTC，拒绝买入"
            ),
        )

    def _check_option_expiry(self, intent: TradeIntent) -> RiskCheckResult:
        """距到期时间必须在配置窗口内（OPT-03）。"""
        instrument = self._option
        delivery = getattr(intent, "delivery_time", None)
        if delivery is None:
            return RiskCheckResult(
                code="OPTION_EXPIRY",
                approved=False,
                detail="缺少到期时间，无法校验到期窗口",
            )
        now = now_utc()
        seconds = (delivery - now).total_seconds()
        days = seconds / 86400
        ok = instrument.min_days_to_expiry <= days <= instrument.max_days_to_expiry
        return RiskCheckResult(
            code="OPTION_EXPIRY",
            approved=ok,
            detail=(
                ""
                if ok
                else f"距到期 {days:.2f} 天不在 [{instrument.min_days_to_expiry}, "
                f"{instrument.max_days_to_expiry}] 区间"
            ),
        )

    def _check_premium_concentration(
        self, intent: TradeIntent, ctx: RiskContext
    ) -> RiskCheckResult:
        """到期集中度：同一标的、同一 UTC 到期日的持仓成本加开仓预留（§5.2 末段）。"""
        underlying = self._underlying_of(intent)
        limits = ctx.validation_limits
        if limits is not None:
            # 验证价差的权利金敞口按协议的首腿支出上限衡量：两腿共用一个
            # 覆盖标的，协议的 cohort 预算已经约束了整段风险。
            after = (
                ctx.premium_used_per_underlying.get(underlying, Decimal("0"))
                + intent.spend_limit
            )
            ok = after <= limits.premium_cap_per_underlying
            return RiskCheckResult(
                code="PREMIUM_CONCENTRATION_UNDERLYING",
                approved=ok,
                detail=(
                    ""
                    if ok
                    else (
                        f"{intent.symbol} 权利金成本将达到 {after}，"
                        f"超过协议上限 {limits.premium_cap_per_underlying}"
                    )
                ),
            )
        underlying_limit = self._limits.premium_cost_limit_per_underlying.get(underlying)
        if underlying_limit is not None:
            after = (
                ctx.premium_used_per_underlying.get(underlying, Decimal("0")) + intent.spend_limit
            )
            if after > underlying_limit.value:
                return RiskCheckResult(
                    code="PREMIUM_CONCENTRATION_UNDERLYING",
                    approved=False,
                    detail=(
                        f"{intent.symbol} 权利金成本将达到 {after}，超过 {underlying_limit.value}"
                    ),
                )

        expiry_key = self._expiry_key(intent)
        expiry_limit = self._limits.premium_cost_limit_per_expiry.get(expiry_key)
        if expiry_limit is not None:
            after = ctx.premium_used_per_expiry.get(expiry_key, Decimal("0")) + intent.spend_limit
            if after > expiry_limit.value:
                return RiskCheckResult(
                    code="PREMIUM_CONCENTRATION_EXPIRY",
                    approved=False,
                    detail=(
                        f"到期日 {expiry_key} 权利金成本将达到 {after}，超过 {expiry_limit.value}"
                    ),
                )
        return RiskCheckResult(code="PREMIUM_CONCENTRATION", approved=True)

    def _expiry_key(self, intent: TradeIntent) -> str:
        delivery = getattr(intent, "delivery_time", None)
        if delivery is None:
            return "unknown"
        return f"{self._underlying_of(intent)}:{delivery.astimezone(dt.UTC):%Y-%m-%d}"

    def _check_delta(self, intent: TradeIntent, ctx: RiskContext) -> Any:
        """按标的做双侧 Delta 区间判定（RSK-05）。"""
        base = self._underlying_of(intent)
        underlyings = {base} | set(ctx.account_delta_per_underlying or {}) | {
            self._underlying_of_symbol(item.symbol) for item in ctx.open_orders
        }
        selected = None
        for underlying in sorted(underlyings):
            limit = self._limits.delta_limit_per_underlying.get(underlying)
            if limit is None:
                from bybitbot.risk.delta import DeltaCheck

                return DeltaCheck(
                    underlying=underlying, d0=Decimal("0"), d_min=Decimal("0"),
                    d_max=Decimal("0"), limit=Decimal("0"), approved=False,
                    reason_code="DELTA_LIMIT_MISSING", detail="未配置该标的 Delta 限额",
                )
            candidates = [DeltaCandidate(
                symbol=item.symbol, underlying=underlying, qty=item.leaves_qty,
                delta_if_filled=item.delta_if_filled, order_id=item.order_id,
                order_link_id=item.order_link_id, leaves_qty=item.leaves_qty,
            ) for item in ctx.open_orders if self._underlying_of_symbol(item.symbol) == underlying]
            if underlying == base:
                candidates.append(self._candidate_for(intent, ctx, underlying))
            check = evaluate_delta_bounds(underlying=underlying,
                d0=self._position_delta(underlying, ctx), candidates=candidates, limit=limit)
            if not check.approved:
                return check
            if underlying == base:
                selected = check
        return selected

    def _candidate_for(
        self, intent: TradeIntent, ctx: RiskContext, underlying: str
    ) -> DeltaCandidate:
        ticker = ctx.quotes.get(intent.symbol)
        if intent.action == IntentAction.SPOT_BUY:
            delta = spot_delta_change(intent.qty, "buy")
        elif intent.action == IntentAction.SPOT_SELL:
            delta = spot_delta_change(intent.qty, "sell")
        else:
            position_delta = ticker.delta if ticker is not None else None
            if position_delta is None:
                # 未知敞口无法量化 —— RSK-05：直接暂停开仓。
                return DeltaCandidate(
                    symbol=intent.symbol,
                    underlying=underlying,
                    qty=intent.qty,
                    delta_if_filled=None,
                )
            delta = self._ctx_units(ctx).to_underlying(position_delta, intent.qty)
            if intent.side.lower() == "sell":
                delta = -delta
        return DeltaCandidate(
            symbol=intent.symbol,
            underlying=underlying,
            qty=intent.qty,
            delta_if_filled=delta,
        )

    def _ctx_units(self, ctx: RiskContext) -> DeltaUnits:
        return ctx.delta_units

    def _position_delta(self, underlying: str, ctx: RiskContext) -> Decimal:
        if ctx.account_delta_per_underlying is not None:
            return ctx.account_delta_per_underlying.get(underlying, Decimal("0"))
        # 仅明确基础币现金和当前每单位 Delta 可组合；缺失单位不能当零风险。
        coin = ctx.account.coin(underlying)
        total = coin.wallet_balance if coin.wallet_balance is not None else Decimal("NaN")
        for position in ctx.positions:
            if self._underlying_of_symbol(position.symbol) != underlying or position.size == 0:
                continue
            if position.category != "option":
                continue
            ticker = ctx.quotes.get(position.symbol)
            if ticker is None or ticker.delta is None:
                return Decimal("NaN")
            total += self._ctx_units(ctx).to_underlying(ticker.delta, position.size) * (
                -1 if (position.side or "").lower() == "sell" else 1
            )
        return total

    def _underlying_of(self, intent: TradeIntent) -> str:
        return intent.underlying or self._underlying_of_symbol(intent.symbol)

    @staticmethod
    def _underlying_of_symbol(symbol: str) -> str:
        """从合约 symbol 推断标的。

        现货 ``BTCUSDT`` -> ``BTC``；期权 ``BTC-24SEP27-65000-P-USDT`` -> ``BTC``。
        """
        if "-" in symbol:
            return symbol.split("-", 1)[0]
        for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
            if symbol.endswith(quote):
                return symbol[: -len(quote)]
        return symbol


def pending_orders_from_orders(
    orders: list[Order],
) -> list[OpenOrderExposure]:
    """把未完成订单转成敞口条目。"""
    result: list[OpenOrderExposure] = []
    for order in orders:
        leaves = order.qty - order.qty_filled
        if leaves <= 0:
            continue
        result.append(
            OpenOrderExposure(
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                leaves_qty=leaves,
                delta_if_filled=Decimal("0"),
                order_link_id=order.order_link_id,
            )
        )
    return result


__all__ = [
    "OpenOrderExposure",
    "RiskCheckResult",
    "RiskContext",
    "RiskEngine",
    "RiskEvaluation",
    "pending_orders_from_orders",
]
