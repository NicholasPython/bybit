"""阶段三自动价差信号的持久化窗口、触发与退出判定。

本模块只生成 :class:`~bybitbot.spread.execution.PairSignal` 和退出请求，
所有物理下单仍由 worker 调用阶段二的受控入口完成；账户、对账、租约、
风险基线和暂停门槛一律不在本模块绕过。样本与止损冷却都落库，重启后
窗口和冷却状态不丢失。
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from bybitbot.adapters.base import Ticker
from bybitbot.clock import now_utc
from bybitbot.db.models import RuntimeState
from bybitbot.money import Rounding, round_to_step
from bybitbot.spread.execution import PairSignal
from bybitbot.spread.models import SpreadPairTrade, SpreadSignalSample
from bybitbot.spread.signals import (
    ACTION_CLOSE,
    ACTION_OPEN,
    ACTION_STOP,
    PriceSample,
    SignalDecision,
    SignalPolicy,
    SignalUnavailable,
    evaluate_signal,
)

ZERO = Decimal("0")
TWO = Decimal(2)
DECISION_KEY = "spread-signal-decision:"
STOP_KEY = "spread-signal-stop:"

logger = logging.getLogger(__name__)
_WARN_AT: dict[str, int] = {}
_WARN_EVERY_MS = 10 * 60 * 1000


def _warn_throttled(pair_key: str, detail: str) -> None:
    """同一配对的信号拒绝日志每 10 分钟最多一条，避免刷屏。"""
    now_ms = int(now_utc().timestamp() * 1000)
    last = _WARN_AT.get(pair_key, 0)
    if now_ms - last < _WARN_EVERY_MS:
        return
    _WARN_AT[pair_key] = now_ms
    logger.warning("自动价差信号被拒：%s：%s", pair_key, detail)

#: 配对编号后缀计数器，进程内单调递增；完整编号仍由阶段二做幂等去重。
_pair_sequence = itertools.count(1)


def _pair_trade_seq(now_ms: int) -> str:
    """同一毫秒多次开仓时用进程内序号区分，避免幂等编号冲突。"""
    return f"{now_ms % 100000:05d}{next(_pair_sequence) % 1000:03d}"[:8]


@dataclass(frozen=True)
class AutoSignalResult:
    """一轮自动信号的输出；无动作时 ``signal`` 与 ``close_pair_id`` 均为空。"""

    decision: SignalDecision | None = None
    signal: PairSignal | None = None
    close_pair_id: str | None = None
    #: 本轮是否写入了新样本；worker 据此控制决策留痕频率。
    sampled: bool = False


class SpreadAutoSignal:
    """把滑动窗口判定接到阶段二执行器之前的纯编排层。

    ``quotes`` 既可以是 ``QuoteCache``（worker 路径），也可以是
    ``{symbol: Ticker}`` 映射（测试路径）。``quote_status`` 是
    ``{symbol: MarketStatus}``，只有双侧都 ``usable`` 才采样，
    避免把过期、交叉或缺深度的行情写进持久化窗口。
    """

    def __init__(self, *, adapter, sessionmaker, environment, account_uid, settings):
        self.adapter = adapter
        self.sessionmaker = sessionmaker
        self.environment = environment
        self.account_uid = account_uid
        self.settings = settings

    @staticmethod
    def _ticker(quotes, symbol: str) -> Ticker | None:
        if isinstance(quotes, Mapping):
            return quotes.get(symbol)
        getter = getattr(quotes, "get_ticker", None)
        return getter("linear", symbol) if getter is not None else None

    @staticmethod
    def _mid(ticker: Ticker | None) -> Decimal | None:
        """用买卖中间价作为价差基准，避免单边报价方向引入固定偏差。"""
        if ticker is None:
            return None
        bid, ask = ticker.bid_price, ticker.ask_price
        if (
            bid is None
            or ask is None
            or not bid.is_finite()
            or not ask.is_finite()
            or bid <= 0
            or ask <= 0
            or bid > ask
        ):
            return None
        return (bid + ask) / TWO

    @staticmethod
    def _execution_price(
        ticker: Ticker, side: str, *, passive: bool
    ) -> tuple[Decimal, Rounding] | None:
        """按腿角色取保护价：被动腿挂在不主动吃单的一侧，主动腿取对手价。"""
        price = ticker.ask_price if side == "Buy" else ticker.bid_price
        if price is None or not price.is_finite() or price <= 0:
            return None
        rounding = (
            (Rounding.UP if side == "Sell" else Rounding.DOWN)
            if passive
            else (Rounding.UP if side == "Buy" else Rounding.DOWN)
        )
        return price, rounding

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

    async def _record(self, config, *, at_ms, ticker_a, ticker_b, specs, quote_status) -> bool:
        price_a = self._mid(ticker_a)
        price_b = self._mid(ticker_b)
        spec_a, spec_b = specs.get(config.symbol_a), specs.get(config.symbol_b)
        # 中间价是推导值，落在半档是正常现象，不作为订单价格下发；此处只要求
        # 双边报价有效，不做 tick 对齐（真实盘口中间价经常是半档）。
        if (
            price_a is None
            or price_b is None
            or spec_a is None
            or spec_b is None
            or not spec_a.tick_size
            or not spec_b.tick_size
            or not spec_a.qty_step
            or not spec_b.qty_step
            or not spec_a.is_tradable
            or not spec_b.is_tradable
            or not quote_status[config.symbol_a].usable
            or not quote_status[config.symbol_b].usable
        ):
            raise SignalUnavailable("自动信号缺少有效双腿报价或合约规格")
        value = {
            "environment": self.environment,
            "pair_key": config.pair_key,
            "at_ms": at_ms,
            "price_a": price_a,
            "price_b": price_b,
            "funding_a": ZERO,
            "funding_b": ZERO,
            "raw": {
                "source": "paper_book",
                "bid_a": str(ticker_a.bid_price),
                "ask_a": str(ticker_a.ask_price),
                "bid_b": str(ticker_b.bid_price),
                "ask_b": str(ticker_b.ask_price),
                "source_ts": {
                    config.symbol_a: ticker_a.source_ts.isoformat() if ticker_a.source_ts else None,
                    config.symbol_b: ticker_b.source_ts.isoformat() if ticker_b.source_ts else None,
                },
            },
        }
        statement = insert(SpreadSignalSample).values(**value)
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

    async def _get_state(self, key: str) -> dict | None:
        async with self.sessionmaker() as session:
            row = await session.scalar(
                select(RuntimeState.value).where(
                    RuntimeState.environment == self.environment, RuntimeState.key == key
                )
            )
        return dict(row) if row is not None else None

    async def _put_state(self, key: str, value: dict) -> None:
        statement = insert(RuntimeState).values(
            environment=self.environment,
            key=key,
            value=value,
            updated_at=now_utc(),
        )
        async with self.sessionmaker() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_runtime_state",
                    set_={
                        "value": statement.excluded.value,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
            await session.commit()

    async def _in_cooldown(self, config, *, now_ms) -> bool:
        value = await self._get_state(STOP_KEY + config.pair_key)
        if value is None:
            return False
        try:
            until_ms = int(value["until_ms"])
        except (KeyError, TypeError, ValueError):
            return False
        return now_ms < until_ms

    @staticmethod
    def _decision_payload(decision: SignalDecision) -> dict:
        return {
            "action": decision.action,
            "reason": decision.reason,
            "direction": decision.direction,
            "z": str(decision.z),
            "spread": str(decision.spread),
            "mean": str(decision.mean),
            "stdev": str(decision.stdev),
            "expected_cost": str(decision.expected_cost),
        }

    async def _record_decision(self, config, *, decision, now_ms) -> None:
        await self._put_state(
            DECISION_KEY + config.pair_key,
            {"at_ms": now_ms, **self._decision_payload(decision)},
        )

    async def _set_cooldown(self, config, *, now_ms, decision) -> None:
        await self._put_state(
            STOP_KEY + config.pair_key,
            {
                "until_ms": now_ms + self.settings.auto.stop_cooldown_seconds * 1000,
                "z": str(decision.z),
                "reason": decision.reason,
            },
        )

    async def _active_pairs(self, config) -> list[SpreadPairTrade]:
        async with self.sessionmaker() as session:
            return list(
                await session.scalars(
                    select(SpreadPairTrade).where(
                        SpreadPairTrade.environment == self.environment,
                        SpreadPairTrade.pair_key == config.pair_key,
                        SpreadPairTrade.closed_at.is_(None),
                    )
                )
            )

    async def evaluate_pair(
        self,
        config,
        *,
        quotes,
        specs,
        quote_status,
        now_ms=None,
    ) -> AutoSignalResult:
        """采样一次并按窗口判定；出错只跳过本轮，不打断 worker 主循环。"""
        if not self.settings.auto.enabled or not config.enabled:
            return AutoSignalResult()
        now = int(now_ms if now_ms is not None else now_utc().timestamp() * 1000)
        auto = self.settings.auto
        ticker_a = self._ticker(quotes, config.symbol_a) or await self.adapter.get_ticker(
            "linear", config.symbol_a
        )
        ticker_b = self._ticker(quotes, config.symbol_b) or await self.adapter.get_ticker(
            "linear", config.symbol_b
        )
        try:
            sampled = await self._record(
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
            # 采样被拒必须留痕：静默吞掉会表现为「信号开启但永远 0 条样本」。
            _warn_throttled(config.pair_key, str(exc))
            return AutoSignalResult()
        if sampled:
            await self._record_decision(config, decision=decision, now_ms=now)
        active = await self._active_pairs(config)

        if decision.action == ACTION_STOP:
            await self._set_cooldown(config, now_ms=now, decision=decision)
            return AutoSignalResult(
                decision=decision,
                close_pair_id=active[0].pair_trade_id if active else None,
                sampled=sampled,
            )
        if decision.action == ACTION_OPEN:
            if active or await self._in_cooldown(config, now_ms=now):
                return AutoSignalResult(decision=decision, sampled=sampled)
            signal = self._build_signal(config, ticker_a, ticker_b, specs, decision, now)
            if signal is None:
                return AutoSignalResult(decision=decision, sampled=sampled)
            return AutoSignalResult(decision=decision, signal=signal, sampled=sampled)
        if decision.action == ACTION_CLOSE and active:
            return AutoSignalResult(
                decision=decision, close_pair_id=active[0].pair_trade_id, sampled=sampled
            )
        return AutoSignalResult(decision=decision, sampled=sampled)

    def _build_signal(
        self,
        config,
        ticker_a: Ticker | None,
        ticker_b: Ticker | None,
        specs,
        decision: SignalDecision,
        now_ms: int,
    ) -> PairSignal | None:
        if ticker_a is None or ticker_b is None:
            return None
        side_a = "Sell" if decision.direction == "SHORT_A_LONG_B" else "Buy"
        side_b = "Buy" if side_a == "Sell" else "Sell"
        chosen_a = self._execution_price(ticker_a, side_a, passive=True)
        chosen_b = self._execution_price(ticker_b, side_b, passive=False)
        if chosen_a is None or chosen_b is None:
            return None
        (price_a, rounding_a), (price_b, rounding_b) = chosen_a, chosen_b
        spec_a, spec_b = specs.get(config.symbol_a), specs.get(config.symbol_b)
        if spec_a is None or spec_b is None or not spec_a.tick_size or not spec_b.tick_size:
            return None
        passive = round_to_step(price_a, spec_a.tick_size, rounding_a)
        active_price = round_to_step(price_b, spec_b.tick_size, rounding_b)
        if passive <= 0 or active_price <= 0:
            return None
        return PairSignal(
            pair_trade_id=f"auto{now_ms:013d}{_pair_trade_seq(now_ms)}",
            pair_key=config.pair_key,
            strategy_id="spread-auto",
            symbol_a=config.symbol_a,
            symbol_b=config.symbol_b,
            direction=decision.direction,
            notional=self.settings.auto.target_notional,
            passive_price=passive,
            active_price=active_price,
            qty_step_a=spec_a.qty_step,
            qty_step_b=spec_b.qty_step,
            side_a=side_a,
            side_b=side_b,
            passive_timeout_ms=config.passive_timeout_ms,
            recovery_window_ms=config.recovery_window_ms,
            max_slippage_bps=config.max_slippage_bps,
            max_unhedged_notional=config.max_unhedged_notional.value,
        )


__all__ = ["AutoSignalResult", "SpreadAutoSignal"]
