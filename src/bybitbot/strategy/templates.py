"""确定性的限价参考策略（STR-01 / STR-03 / SPT-01 / OPT-03）。

只生成草稿；数量按最新规格向下舍入，预算包含保守手续费。默认不启用。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bybitbot.fees import FeeModel
from bybitbot.money import Rounding, round_to_step
from bybitbot.strategy.base import IntentDraft, SignalContext, StrategyDeps


@dataclass
class SpotBuyParams:
    symbol: str
    budget_per_run: Decimal
    currency: str = "USDT"
    max_price_deviation: Decimal = Decimal("0.005")
    min_qty: Decimal = Decimal("0")
    ttl_seconds: int = 300
    enabled: bool = False
    fee_rate: Decimal = Decimal("0.001")


@dataclass
class OptionBuyParams:
    underlying: str
    currency: str = "USDT"
    options_type: str | None = None
    min_days_to_expiry: int = 1
    max_days_to_expiry: int = 45
    target_delta: Decimal | None = None
    delta_tolerance: Decimal = Decimal("0.25")
    strike_range: tuple[Decimal, Decimal] | None = None
    premium_budget: Decimal = Decimal("0")
    min_premium: Decimal = Decimal("0")
    max_premium: Decimal = Decimal("0")
    min_open_interest: Decimal = Decimal("0")
    max_spread_ratio: Decimal = Decimal("0.05")
    ttl_seconds: int = 120
    enabled: bool = False
    #: 期权费用口径（CR-02）：按官方公式随标的指数价变化，
    #: 缺指数价时按封顶比例估算（保守方向）。
    fee_model: FeeModel = field(default_factory=FeeModel.conservative_default)
    allowed_types: frozenset[str] = frozenset({"Call", "Put"})


@dataclass
class ExitRuleParams:
    hold_seconds: int | None = None
    profit_threshold: Decimal | None = None
    exit_before_expiry_seconds: int | None = None
    price_floor_ratio: Decimal = Decimal("0")
    max_chase: int = 1


def exit_rules(params: ExitRuleParams) -> tuple[dict[str, object], ...]:
    rules: list[dict[str, object]] = []
    if params.hold_seconds is not None:
        rules.append({"kind": "hold_period", "hold_seconds": params.hold_seconds})
    if params.profit_threshold is not None:
        rules.append(
            {"kind": "profit_threshold", "profit_threshold_pct": str(params.profit_threshold * 100)}
        )
    if params.exit_before_expiry_seconds is not None:
        rules.append(
            {"kind": "before_expiry", "seconds_before_expiry": params.exit_before_expiry_seconds}
        )
    return tuple(rules)


def _sized(instrument, price: Decimal, budget: Decimal, fee_rate: Decimal) -> Decimal:
    if not instrument.qty_step or not instrument.tick_size:
        return Decimal("0")
    qty = round_to_step(budget / (price * (1 + fee_rate)), instrument.qty_step, Rounding.DOWN)
    if instrument.max_order_qty is not None:
        qty = min(qty, round_to_step(instrument.max_order_qty, instrument.qty_step, Rounding.DOWN))
    if qty <= 0 or (instrument.min_order_qty is not None and qty < instrument.min_order_qty):
        return Decimal("0")
    if instrument.min_order_amt is not None and qty * price < instrument.min_order_amt:
        return Decimal("0")
    return qty


class ScheduledSpotBuyStrategy:
    kind = "spot_fixed_budget_dca"

    def __init__(self, name: str, params: SpotBuyParams) -> None:
        self.name, self.params = name, params

    def evaluate(self, ctx: SignalContext, deps: StrategyDeps) -> list[IntentDraft]:
        p = self.params
        if not p.enabled:
            return []
        instrument = deps.instrument("spot", p.symbol)
        quote = deps.quote("spot", p.symbol)
        if (
            instrument is None
            or not instrument.is_tradable
            or not instrument.tick_size
            or instrument.quote_coin != p.currency
            or quote is None
            or quote.ask_price is None
            or quote.ask_price <= 0
            or quote.is_crossed
        ):
            return []
        price = round_to_step(
            quote.ask_price * (1 + p.max_price_deviation), instrument.tick_size, Rounding.DOWN
        )
        if price <= 0:
            return []
        qty = _sized(instrument, price, p.budget_per_run, p.fee_rate)
        if qty <= 0 or qty < p.min_qty:
            return []
        return [
            IntentDraft(
                action="SPOT_BUY",
                category="spot",
                symbol=p.symbol,
                side="Buy",
                qty=qty,
                qty_unit="base_coin",
                currency=p.currency,
                spend_limit=p.budget_per_run,
                price_limit=price,
                underlying=instrument.base_coin,
                ttl_seconds=p.ttl_seconds,
                rationale={
                    "ask": str(quote.ask_price),
                    "budget": str(p.budget_per_run),
                    "fee_rate": str(p.fee_rate),
                    "qty_step": str(instrument.qty_step),
                },
            )
        ]


class ScheduledOptionBuyStrategy:
    kind = "option_single_leg_buy"

    def __init__(self, name: str, params: OptionBuyParams) -> None:
        self.name, self.params = name, params

    def evaluate(self, ctx: SignalContext, deps: StrategyDeps) -> list[IntentDraft]:
        p = self.params
        if not p.enabled:
            return []
        candidates = self._candidates(deps, deps.now)
        candidates.sort(key=lambda c: (c["premium"], c["expiry"], c["symbol"]))
        if not candidates:
            return []
        best = candidates[0]
        spec = best["instrument"]
        price = round_to_step(best["premium"], spec.tick_size, Rounding.DOWN)
        ratio = self._fee_ratio(p, price, best["index_price"])
        qty = _sized(spec, price, p.premium_budget, ratio)
        if qty <= 0:
            return []
        return [
            IntentDraft(
                action="OPTION_BUY_OPEN",
                category="option",
                symbol=spec.symbol,
                side="Buy",
                qty=qty,
                qty_unit="contract",
                currency=spec.settle_coin,
                spend_limit=p.premium_budget,
                price_limit=price,
                underlying=spec.base_coin,
                delivery_time=spec.delivery_time,
                strike_price=spec.strike_price,
                options_type=spec.options_type,
                ttl_seconds=p.ttl_seconds,
                rationale={
                    "candidates": len(candidates),
                    "chosen": spec.symbol,
                    "sort_rule": "ask asc, expiry asc, symbol asc",
                    "delta": str(best["delta"]),
                    "fee_rate": str(ratio),
                    "fee_version": p.fee_model.version,
                },
            )
        ]

    def _fee_ratio(self, params: OptionBuyParams, price: Decimal, index_price) -> Decimal:
        """期权费用占权利金的比例（CR-02）。"""
        return params.fee_model.option_fee_ratio(option_price=price, index_price=index_price)

    def _candidates(self, deps: StrategyDeps, now: dt.datetime) -> list[dict[str, Any]]:
        p = self.params
        out = []
        for spec in deps.instruments("option"):
            if (
                not spec.is_tradable
                or spec.base_coin != p.underlying
                or spec.settle_coin != p.currency
                or not spec.tick_size
                or not spec.qty_step
                or spec.strike_price is None
                or spec.delivery_time is None
                or spec.options_type not in p.allowed_types
                or (p.options_type is not None and spec.options_type != p.options_type)
            ):
                continue
            days = Decimal(str((spec.delivery_time - now).total_seconds())) / 86400
            if not p.min_days_to_expiry <= days <= p.max_days_to_expiry:
                continue
            quote = deps.quote("option", spec.symbol)
            if (
                quote is None
                or quote.ask_price is None
                or quote.bid_price is None
                or quote.ask_price <= 0
                or quote.bid_price < 0
                or quote.is_crossed
                or quote.delta is None
                or quote.iv is None
            ):
                continue
            if (quote.ask_price - quote.bid_price) / quote.ask_price > p.max_spread_ratio:
                continue
            if quote.ask_size is None or quote.ask_size <= 0:
                continue
            if p.min_open_interest and (
                quote.open_interest is None or quote.open_interest < p.min_open_interest
            ):
                continue
            if p.target_delta is not None and abs(abs(quote.delta) - abs(p.target_delta)) > (
                p.delta_tolerance
            ):
                continue
            if p.max_premium > 0 and quote.ask_price > p.max_premium:
                continue
            if quote.ask_price < p.min_premium:
                continue
            if p.strike_range is not None:
                underlying_quote = deps.quote("spot", f"{p.underlying}{p.currency}")
                if (
                    underlying_quote is None
                    or underlying_quote.ask_price is None
                    or underlying_quote.ask_price <= 0
                ):
                    continue
                ratio = spec.strike_price / underlying_quote.ask_price
                if not p.strike_range[0] <= ratio <= p.strike_range[1]:
                    continue
            out.append(
                {
                    "instrument": spec,
                    "premium": quote.ask_price,
                    "expiry": spec.delivery_time,
                    "symbol": spec.symbol,
                    "delta": quote.delta,
                    "index_price": getattr(quote, "index_price", None),
                }
            )
        return out
