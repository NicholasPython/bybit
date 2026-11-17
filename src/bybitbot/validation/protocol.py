"""研究口径阈值（公共层）。

两个前向研究协议把阈值写在 JSON 里：

* ``research/2026-11-15-bull-call-forward/protocol.json``
* ``research/2026-11-15-options-forward/protocol.json``

研究引擎与控制台此前各读各的。这里把**参与决策的阈值**收敛成一个不可变
对象：产品内的策略、验证任务与研究重放读同一份 :class:`SpreadProtocol`，
``from_mapping`` 可直读冻结的看涨协议；看跌协议的逐笔预算语义不同，显式拒绝。

这些阈值描述的是 **2026-11 的观察协议**，不是实盘参数；它们描述的是
"当时怎么判断"，不是"现在该怎么做"。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation

from bybitbot.terms import option_fee

#: 一小时（毫秒）。
HOUR_MS = 3_600_000
#: 一天（毫秒）。
DAY_MS = 24 * HOUR_MS

_ZERO = Decimal("0")


@dataclass(frozen=True)
class SpreadProtocol:
    """两腿价差研究的阈值口径。

    字段名与 ``bull-call-forward/protocol.json`` 同名，便于直接读取冻结协议。
    缺字段使用默认值；只忽略已知元数据，未知阈值与不兼容协议报错。
    直接构造与映射解析都校验数值、区间和观察时序。
    """

    # ---- 观察窗口 ----
    sampling_seconds: int = 900
    max_observation_gap_seconds: int = 1800
    max_response_age_seconds: int = 30
    pending_ttl_seconds: int = 1800
    end_exit_buffer_seconds: int = 3600
    study_days: int = 5

    # ---- 持有与到期 ----
    max_hold_hours: int = 72
    close_before_expiry_hours: int = 24

    # ---- 入场窗口 ----
    min_entry_days: int = 10
    max_entry_days: int = 40
    target_entry_days: int = 21

    # ---- Delta 带 ----
    long_delta_min: Decimal = Decimal("0.50")
    long_delta_max: Decimal = Decimal("0.65")
    short_delta_min: Decimal = Decimal("0.25")
    short_delta_max: Decimal = Decimal("0.40")
    target_long_delta: Decimal = Decimal("0.55")
    target_short_delta: Decimal = Decimal("0.35")

    # ---- 宽度带（行权价差 / 标的指数价）----
    width_index_min: Decimal = Decimal("0.03")
    width_index_max: Decimal = Decimal("0.08")

    # ---- 预算 ----
    aggregate_risk_budget_usdt: Decimal = Decimal("25")
    max_first_leg_outlay_usdt: Decimal = Decimal("100")

    # ---- 成交与成本闸门 ----
    max_spread_fraction: Decimal = Decimal("0.05")
    entry_limit_fraction: Decimal = Decimal("0.02")
    adverse_slippage_ticks: int = 1
    stress_adverse_ticks: int = 2
    taker_rate: Decimal = Decimal("0.0003")
    premium_fee_cap: Decimal = Decimal("0.07")
    min_reward_risk: Decimal = Decimal("0.25")
    max_entry_friction_profit: Decimal = Decimal("0.15")
    max_roundtrip_loss_risk: Decimal = Decimal("0.15")
    max_roundtrip_loss_profit: Decimal = Decimal("0.25")
    take_profit_debit_fraction: Decimal = Decimal("0.50")
    stop_loss_debit_fraction: Decimal = Decimal("0.50")

    def __post_init__(self) -> None:
        for spec in fields(self):
            value = _coerce(type(spec.default), getattr(self, spec.name), spec.name)
            object.__setattr__(self, spec.name, value)
            if value < 0 or (value == 0 and spec.name not in _ZERO_ALLOWED):
                raise ValueError(f"{spec.name} must be positive")

        for name in _UNIT_FRACTIONS:
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must be at most 1")
        for prefix in ("long", "short"):
            low = getattr(self, f"{prefix}_delta_min")
            target = getattr(self, f"target_{prefix}_delta")
            high = getattr(self, f"{prefix}_delta_max")
            if not 0 <= low <= target <= high <= 1:
                raise ValueError(f"{prefix} delta must satisfy 0 <= min <= target <= max <= 1")
        if not self.min_entry_days <= self.target_entry_days <= self.max_entry_days:
            raise ValueError("entry days must satisfy min <= target <= max")
        if not self.width_index_min <= self.width_index_max < 1:
            raise ValueError("width index must satisfy 0 < min <= max < 1")
        if self.stress_adverse_ticks < self.adverse_slippage_ticks:
            raise ValueError("stress_adverse_ticks must be >= adverse_slippage_ticks")
        if self.max_observation_gap_seconds < self.sampling_seconds:
            raise ValueError("max_observation_gap_seconds must be >= sampling_seconds")
        if self.pending_ttl_seconds < self.sampling_seconds:
            raise ValueError("pending_ttl_seconds must be >= sampling_seconds")
        if self.max_response_age_seconds > self.sampling_seconds:
            raise ValueError("max_response_age_seconds must be <= sampling_seconds")
        if not self.sampling_seconds <= self.end_exit_buffer_seconds < self.study_days * 86400:
            raise ValueError("end_exit_buffer_seconds must fit sampling and study duration")
        if self.close_before_expiry_hours >= self.min_entry_days * 24:
            raise ValueError("close_before_expiry_hours must be below minimum entry duration")

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> SpreadProtocol:
        """读取看涨协议；保留默认阈值，拒绝未知决策字段与不兼容模式。"""
        if not isinstance(data, Mapping):
            raise ValueError("protocol must be a mapping")
        if "version" in data and data["version"] != "conditional-bull-call-forward-v1":
            raise ValueError(
                "unsupported protocol version; bear per-trade budgets are incompatible"
            )
        if "mode" in data and data["mode"] != "research_paper_only":
            raise ValueError("unsupported protocol mode")
        names = {spec.name for spec in fields(cls)}
        unknown = data.keys() - names - _METADATA
        if unknown:
            raise ValueError(f"unknown or incompatible protocol fields: {sorted(unknown, key=str)}")
        return cls(**{name: data[name] for name in names if name in data})  # type: ignore[arg-type]

    def option_fee(self, *, premium: Decimal, qty: Decimal, index_price: Decimal | None) -> Decimal:
        """按协议费率估算一条腿的费用。

        复用公共层的 :func:`~bybitbot.terms.option_fee`：
        ``qty × min(指数价 × taker_rate, 权利金 × premium_fee_cap)``。
        """
        return option_fee(
            premium=premium,
            qty=qty,
            index_price=index_price,
            underlying_rate=self.taker_rate,
            premium_cap_rate=self.premium_fee_cap,
        )

    @property
    def entry_window_ms(self) -> tuple[int, int]:
        """入场窗口（毫秒，相对到期时刻的提前量区间）。"""
        return (self.min_entry_days * DAY_MS, self.max_entry_days * DAY_MS)

    @property
    def target_expiry_offset_ms(self) -> int:
        """目标到期距离（毫秒）—— 候选排序的首要依据。"""
        return self.target_entry_days * DAY_MS


_METADATA = frozenset(
    {
        "version",
        "mode",
        "bases",
        "opposing_run",
        "opposing_protocol_hash",
        "opposing_engine_hash",
        "description",
        "notes",
        "metadata",
    }
)
_UNIT_FRACTIONS = frozenset(
    {
        "max_spread_fraction",
        "entry_limit_fraction",
        "taker_rate",
        "premium_fee_cap",
        "max_entry_friction_profit",
        "max_roundtrip_loss_risk",
        "max_roundtrip_loss_profit",
        "stop_loss_debit_fraction",
    }
)
_ZERO_ALLOWED = _UNIT_FRACTIONS | {"adverse_slippage_ticks", "stress_adverse_ticks"}


def _coerce(kind: type, value: object, name: str) -> Decimal | int:
    """JSON 数字和数字字符串都可用；禁止布尔、截断与非有限数。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not bool")
    if kind is int:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                pass
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, (Decimal, int, float, str)):
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            pass
        else:
            if result.is_finite():
                return result
    raise ValueError(f"{name} must be a finite decimal")


__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "SpreadProtocol",
]
