"""FR-07.3 影子盘：公开行情 + 真实可见深度 + 纯模拟成交。

严格边界：

* 只接受 ``BybitRestAdapter`` 风格的公开只读适配器；不读取交易凭据，
  不接触私有流，不调用任何交易写接口；
* 样本与 phase-three 滚动信号共用口径，但落在独立的 ``shadow`` 环境；
* 模拟成交按盘口逐档消耗，带冲击成本和手续费，不模拟资金费率、保证金、
  清算、排队或交易所撮合延迟；
* 只产生模拟 PnL 与晋升候选，绝不修改配置、不下发灰度或实盘指令。
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from bybitbot.adapters.base import OrderBook, Ticker
from bybitbot.clock import now_utc
from bybitbot.money import Rounding, quantize_qty, round_to_step
from bybitbot.spread.models import ShadowTrade, SpreadSignalSample
from bybitbot.spread.signals import (
    ACTION_CLOSE,
    ACTION_NONE,
    ACTION_OPEN,
    ACTION_STOP,
    PriceSample,
    SignalDecision,
    SignalPolicy,
    SignalUnavailable,
    evaluate_signal,
)
from bybitbot.spread.states import log_spread

ZERO = Decimal(0)
TWO = Decimal(2)
ENVIRONMENT = "shadow"
_SHADOW_SEQUENCE = itertools.count(1)

logger = logging.getLogger(__name__)
_WARN_AT: dict[str, int] = {}
_WARN_EVERY_MS = 10 * 60 * 1000


def _warn_throttled(pair_key: str, detail: str) -> None:
    """同一配对的采样拒绝日志每 10 分钟最多一条，避免刷屏。"""
    now_ms = int(now_utc().timestamp() * 1000)
    last = _WARN_AT.get(pair_key, 0)
    if now_ms - last < _WARN_EVERY_MS:
        return
    _WARN_AT[pair_key] = now_ms
    logger.warning("影子盘采样被拒：%s：%s", pair_key, detail)


def _finite(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ValueError(f"{name} 必须是有限 Decimal")
    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{name} 必须是有限 Decimal")
    return result


@dataclass(frozen=True)
class Fill:
    price: Decimal
    qty: Decimal
    fee: Decimal
    slippage_bps: Decimal
    depth_ok: bool


@dataclass(frozen=True)
class ShadowResult:
    decision: SignalDecision
    row: ShadowTrade
    depth_ok: bool


class ShadowEngine:
    """影子盘采样、模拟撮合与统计；每个实例只对应一个环境命名空间。"""

    def __init__(self, *, adapter, sessionmaker, settings, environment: str = ENVIRONMENT):
        if environment != ENVIRONMENT:
            raise ValueError("影子盘只能使用 shadow 环境")
        methods = tuple(getattr(adapter, name, None) for name in ("get_ticker", "get_orderbook"))
        if not all(callable(method) for method in methods):
            raise ValueError("影子盘只接受提供公开行情只读接口的适配器")
        write = getattr(adapter, "allow_trade_writes", None)
        if write is not None and write is not False:
            raise ValueError("影子盘适配器必须显式禁用交易写（allow_trade_writes=False）")
        self.adapter = adapter
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.environment = environment

    @staticmethod
    def _ticker(quotes, symbol: str) -> Ticker | None:
        if isinstance(quotes, Mapping):
            return quotes.get(symbol)
        getter = getattr(quotes, "get_ticker", None)
        return getter("linear", symbol) if getter is not None else None

    @staticmethod
    def _book(quotes, symbol: str) -> OrderBook | None:
        if isinstance(quotes, Mapping):
            return quotes.get(symbol)
        getter = getattr(quotes, "get_book", None)
        return getter("linear", symbol) if getter is not None else None

    @staticmethod
    def _mid(ticker: Ticker | None) -> Decimal | None:
        if ticker is None or ticker.bid_price is None or ticker.ask_price is None:
            return None
        bid, ask = ticker.bid_price, ticker.ask_price
        if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0 or bid > ask:
            return None
        return (bid + ask) / TWO

    def _policy(self, config) -> SignalPolicy:
        auto = self.settings.auto
        return SignalPolicy(
            window=auto.window,
            entry_z=config.z_entry,
            exit_z=config.z_exit,
            stop_z=config.z_stop,
            fee_rate=auto.fee_rate,
            slippage_bps=auto.slippage_bps,
            funding_buffer=auto.funding_buffer,
            max_signal_age_ms=auto.max_signal_age_ms,
        )

    async def _record_sample(self, config, *, at_ms, ticker_a, ticker_b, specs, quote_status):
        price_a, price_b = self._mid(ticker_a), self._mid(ticker_b)
        spec_a, spec_b = specs.get(config.symbol_a), specs.get(config.symbol_b)
        # 中间价是推导值，落在半档（如 bid=97000.0/ask=97000.1 → mid=97000.05）是
        # 正常现象，不作为订单价格下发；此处只要求双边报价有效，不做 tick 对齐。
        # 拒绝时逐项给出原因，避免「0 条样本」只能靠猜。
        problems = []
        if price_a is None:
            problems.append(f"{config.symbol_a}:无有效中间价")
        if price_b is None:
            problems.append(f"{config.symbol_b}:无有效中间价")
        if spec_a is None:
            problems.append(f"{config.symbol_a}:缺合约规格")
        else:
            if not spec_a.tick_size:
                problems.append(f"{config.symbol_a}:tick_size为空")
            if not spec_a.is_tradable:
                problems.append(f"{config.symbol_a}:不可交易({spec_a.status})")
        if spec_b is None:
            problems.append(f"{config.symbol_b}:缺合约规格")
        else:
            if not spec_b.tick_size:
                problems.append(f"{config.symbol_b}:tick_size为空")
            if not spec_b.is_tradable:
                problems.append(f"{config.symbol_b}:不可交易({spec_b.status})")
        if not quote_status.get(config.symbol_a, False):
            problems.append(f"{config.symbol_a}:行情过期或缺失")
        if not quote_status.get(config.symbol_b, False):
            problems.append(f"{config.symbol_b}:行情过期或缺失")
        if problems:
            raise SignalUnavailable("；".join(problems))
        statement = insert(SpreadSignalSample).values(
            environment=self.environment,
            pair_key=config.pair_key,
            at_ms=at_ms,
            price_a=price_a,
            price_b=price_b,
            funding_a=ZERO,
            funding_b=ZERO,
            raw={
                "source": "shadow_public_book",
                "bid_a": str(ticker_a.bid_price),
                "ask_a": str(ticker_a.ask_price),
                "bid_b": str(ticker_b.bid_price),
                "ask_b": str(ticker_b.ask_price),
                "credential_ref": None,
                "trade_writes": False,
            },
        )
        async with self.sessionmaker() as session:
            result = await session.execute(
                statement.on_conflict_do_nothing(constraint="uq_spread_signal_sample").returning(
                    SpreadSignalSample.id
                )
            )
            await session.commit()
        return result.scalar() is not None

    async def _samples(self, config, *, window, now_ms) -> list[PriceSample]:
        async with self.sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(SpreadSignalSample)
                    .where(
                        SpreadSignalSample.environment == self.environment,
                        SpreadSignalSample.pair_key == config.pair_key,
                        SpreadSignalSample.at_ms <= now_ms,
                    )
                    .order_by(SpreadSignalSample.at_ms.desc())
                    .limit(window)
                )
            )
        return [
            PriceSample(
                at_ms=row.at_ms,
                price_a=row.price_a,
                price_b=row.price_b,
                funding_a=row.funding_a,
                funding_b=row.funding_b,
            )
            for row in reversed(rows)
        ]

    def _simulate_fill(
        self, *, book: OrderBook | None, side: str, notional: Decimal, spec, reference: Decimal
    ) -> Fill:
        """按可见档位逐档吃单；保护价按最差一档加上冲击成本。"""
        if book is None or not book.bids or not book.asks or book.gap_detected:
            return Fill(ZERO, ZERO, ZERO, ZERO, False)
        levels = book.asks if side == "Buy" else book.bids
        if any(price <= 0 or qty < 0 for price, qty in levels):
            return Fill(ZERO, ZERO, ZERO, ZERO, False)
        remaining, cost = notional, ZERO
        worst = reference
        for price, size in levels:
            if remaining <= ZERO:
                break
            available = price * size
            used = min(remaining, available)
            cost += used
            remaining -= used
            worst = price
        if remaining > ZERO:
            return Fill(ZERO, ZERO, ZERO, ZERO, False)
        impact = self.settings.shadow.impact_bps_per_depth / Decimal(10000)
        limit = worst * (1 + impact) if side == "Buy" else worst * (1 - impact)
        price = round_to_step(
            limit, spec.tick_size, Rounding.UP if side == "Buy" else Rounding.DOWN
        )
        qty = quantize_qty(notional / price, spec.qty_step)
        if price <= 0 or qty <= 0:
            return Fill(ZERO, ZERO, ZERO, ZERO, False)
        slip = (price - reference).copy_abs() / reference * Decimal(10000)
        return Fill(price, qty, notional * self.settings.auto.fee_rate, slip, True)

    async def _active(self, config):
        async with self.sessionmaker() as session:
            return await session.scalar(
                select(ShadowTrade)
                .where(
                    ShadowTrade.environment == self.environment,
                    ShadowTrade.pair_key == config.pair_key,
                    ShadowTrade.action.in_((ACTION_OPEN, ACTION_NONE)),
                    ShadowTrade.qty_a > 0,
                )
                .order_by(ShadowTrade.decision_at_ms.desc())
                .limit(1)
            )

    async def evaluate_pair(
        self,
        config,
        *,
        quotes,
        specs,
        quote_status,
        books=None,
        now_ms=None,
    ) -> ShadowResult | None:
        """采样一次、生成纯模拟结果；不产生真实订单或账户变更。

        配对开关（``config.enabled``）只控制真实执行面；影子盘只要自身
        启用就会对 ``spread.pairs`` 里声明的配对持续采样。
        """
        if not self.settings.shadow.enabled:
            return None
        auto = self.settings.auto
        now = int(now_ms if now_ms is not None else now_utc().timestamp() * 1000)
        ticker_a = self._ticker(quotes, config.symbol_a) or await self.adapter.get_ticker(
            "linear", config.symbol_a
        )
        ticker_b = self._ticker(quotes, config.symbol_b) or await self.adapter.get_ticker(
            "linear", config.symbol_b
        )
        try:
            await self._record_sample(
                config,
                at_ms=now
                // (auto.sample_interval_seconds * 1000)
                * (auto.sample_interval_seconds * 1000),
                ticker_a=ticker_a,
                ticker_b=ticker_b,
                specs=specs,
                quote_status=quote_status,
            )
            samples = await self._samples(config, window=auto.window, now_ms=now)
            decision = evaluate_signal(
                samples,
                self._policy(config),
                hedge_ratio=config.hedge_ratio,
                notional=auto.target_notional,
                now_ms=now,
            )
        except SignalUnavailable as exc:
            # 采样被拒必须留痕：静默吞掉会表现为「影子盘开启但永远 0 条样本」。
            _warn_throttled(config.pair_key, str(exc))
            return None
        active = await self._active(config)
        direction = decision.direction or (active.direction if active else "")
        side_a = side_b = ""
        spread_pnl = funding_pnl = ZERO
        qty_a = qty_b = price_a_value = price_b_value = ZERO
        fee_cost = slippage_cost = ZERO
        depth_ok = False
        notional = ZERO

        if decision.action == ACTION_OPEN and active is None:
            direction = decision.direction
            side_a = "Sell" if direction == "SHORT_A_LONG_B" else "Buy"
            side_b = "Buy" if side_a == "Sell" else "Sell"
            notional = auto.target_notional
            spec_a, spec_b = specs[config.symbol_a], specs[config.symbol_b]
            book_a = self._book(books, config.symbol_a) if books is not None else None
            book_b = self._book(books, config.symbol_b) if books is not None else None
            if book_a is None:
                book_a = await self.adapter.get_orderbook("linear", config.symbol_a)
            if book_b is None:
                book_b = await self.adapter.get_orderbook("linear", config.symbol_b)
            reference_a, reference_b = self._mid(ticker_a), self._mid(ticker_b)
            fill_a = self._simulate_fill(
                book=book_a, side=side_a, notional=notional, spec=spec_a, reference=reference_a
            )
            fill_b = self._simulate_fill(
                book=book_b, side=side_b, notional=notional, spec=spec_b, reference=reference_b
            )
            depth_ok = fill_a.depth_ok and fill_b.depth_ok
            if depth_ok and reference_a and reference_b:
                qty_a, price_a_value = fill_a.qty, fill_a.price
                qty_b, price_b_value = fill_b.qty, fill_b.price
                fee_cost = fill_a.fee + fill_b.fee
                slippage_cost = (
                    (fill_a.slippage_bps + fill_b.slippage_bps) / Decimal(10000) * notional
                )
        elif decision.action in (ACTION_CLOSE, ACTION_STOP) and active is not None:
            direction, side_a, side_b = active.direction, active.side_a, active.side_b
            entry = log_spread(
                active.price_a if active.price_a > 0 else self._mid(ticker_a),
                active.price_b if active.price_b > 0 else self._mid(ticker_b),
                config.hedge_ratio,
            )
            exit_spread = log_spread(self._mid(ticker_a), self._mid(ticker_b), config.hedge_ratio)
            multiplier = 1 if direction == "SHORT_A_LONG_B" else -1
            spread_pnl = (entry - exit_spread) * multiplier * active.notional
            notional = active.notional
            qty_a, qty_b = active.qty_a, active.qty_b
            fee_cost = active.fee_cost
            slippage_cost = active.slippage_cost
            funding_pnl = (
                active.context.get("funding_a", 0) - active.context.get("funding_b", 0)
            ) * multiplier
            depth_ok = bool(active.depth_ok)

        net_pnl = spread_pnl + funding_pnl - fee_cost - slippage_cost
        async with self.sessionmaker() as session:
            previous = await session.scalar(
                select(func.coalesce(func.max(ShadowTrade.cumulative_pnl), 0)).where(
                    ShadowTrade.environment == self.environment
                )
            )
            cumulative = previous + net_pnl
            suffix = f"{now % 100000:05d}{next(_SHADOW_SEQUENCE) % 1000:03d}"[:8]
            row = ShadowTrade(
                environment=self.environment,
                pair_trade_id=f"shadow{now:013d}{suffix}",
                pair_key=config.pair_key,
                decision_at_ms=now,
                action=decision.action,
                reason=decision.reason,
                direction=direction,
                symbol_a=config.symbol_a,
                symbol_b=config.symbol_b,
                side_a=side_a,
                side_b=side_b,
                qty_a=qty_a,
                qty_b=qty_b,
                price_a=price_a_value,
                price_b=price_b_value,
                notional=notional,
                z=decision.z,
                spread_pnl=spread_pnl,
                fee_cost=fee_cost,
                slippage_cost=slippage_cost,
                funding_pnl=funding_pnl,
                net_pnl=net_pnl,
                cumulative_pnl=cumulative,
                depth_ok=depth_ok,
                context={
                    "simulated": True,
                    "environment": ENVIRONMENT,
                    "credential_ref": None,
                    "trade_writes": False,
                    "reason": decision.reason,
                },
            )
            session.add(row)
            await session.commit()
        return ShadowResult(decision=decision, row=row, depth_ok=depth_ok)

    async def status(self) -> dict[str, object]:
        """投影 FR-07.3 验收指标与人工审阅候选；不触发任何晋升动作。"""
        since = now_utc() - dt.timedelta(days=self.settings.shadow.observation_days)
        since_ms = int(since.timestamp() * 1000)
        async with self.sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(ShadowTrade)
                    .where(
                        ShadowTrade.environment == self.environment,
                        ShadowTrade.decision_at_ms >= since_ms,
                    )
                    .order_by(ShadowTrade.decision_at_ms)
                )
            )
        pnls = [row.net_pnl for row in rows if row.action in (ACTION_CLOSE, ACTION_STOP)]
        peak, max_dd = ZERO, ZERO
        equity = ZERO
        daily: dict[str, Decimal] = {}
        for row in rows:
            equity += row.net_pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else ZERO)
            day = dt.datetime.fromtimestamp(row.decision_at_ms / 1000, tz=dt.UTC).date().isoformat()
            daily[day] = daily.get(day, ZERO) + row.net_pnl
        mean = sum(pnls) / len(pnls) if pnls else ZERO
        variance = sum((value - mean) ** 2 for value in pnls) / len(pnls) if len(pnls) > 1 else ZERO
        sharpe = mean / variance.sqrt() * Decimal("252").sqrt() if variance > 0 else ZERO
        candidate = bool(
            len(pnls) >= 5
            and sharpe > self.settings.shadow.min_sharpe
            and max_dd < self.settings.shadow.max_drawdown
        )
        return {
            "enabled": self.settings.shadow.enabled,
            "observation_days": self.settings.shadow.observation_days,
            "since": _timestamp(since),
            "decisions": len(rows),
            "closed_trades": len(pnls),
            "sharpe": str(sharpe),
            "max_drawdown": str(max_dd),
            "cumulative_pnl": str(sum(pnls)),
            "daily_pnl": {day: str(value) for day, value in sorted(daily.items())},
            "promotion_candidate": candidate,
            "promotion_note": ("仅生成人工审阅候选；影子盘不自动改参数、不灰度、不实盘晋级。"),
            "last_decisions": [
                {
                    "pair_key": row.pair_key,
                    "action": row.action,
                    "reason": row.reason,
                    "net_pnl": str(row.net_pnl),
                    "at_ms": row.decision_at_ms,
                    "depth_ok": row.depth_ok,
                }
                for row in rows[-20:]
            ],
        }


def _timestamp(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["ENVIRONMENT", "Fill", "ShadowEngine", "ShadowResult"]
