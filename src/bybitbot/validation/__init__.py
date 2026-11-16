"""研究前向账本能力在产品侧的纯函数层。

这个包把两个前向研究（``research/2026-11-15-bull-call-forward``、
``research/2026-11-15-options-forward``）里的判断口径搬进公共层，供交易
系统的策略、验证任务与研究重放共用：

* :mod:`~bybitbot.validation.indicators` —— SMA、24 小时收益与两套信号口径；
* :mod:`~bybitbot.validation.spread` —— 两腿结构、可执行价格、价差级成本
  闸门与候选选择；
* :mod:`~bybitbot.validation.position` —— per-base 阶段状态机与现金/费用
  累计；
* :mod:`~bybitbot.validation.observation` —— 观察缺口记账；
* :mod:`~bybitbot.validation.protocol` —— 阈值口径（可直读冻结的协议 JSON）。

**这里只做判断，不做交易**：没有下单、没有写库、没有凭据。指标与价差计算为
纯函数；PositionLedger 只修改自身内存状态，持久化与执行由调用方负责。
"""

from bybitbot.validation.indicators import (
    Signal,
    SignalPolicy,
    evaluate_signal,
    sma,
)
from bybitbot.validation.observation import ObservationGap, detect_gap
from bybitbot.validation.position import (
    TERMINAL_PHASES,
    TRANSITIONS,
    ExitQuote,
    FillRecord,
    IntentPurpose,
    PendingIntent,
    PositionLedger,
    StudyPhase,
)
from bybitbot.validation.protocol import DAY_MS, HOUR_MS, SpreadProtocol
from bybitbot.validation.spread import (
    SpreadCandidate,
    SpreadEconomics,
    SpreadLeg,
    bear_contract_ok,
    choose_candidate,
    executable_price,
    limit_price,
    remaining_risk_budget,
    spread_economics,
    structure_ok,
)

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "TERMINAL_PHASES",
    "TRANSITIONS",
    "ExitQuote",
    "FillRecord",
    "IntentPurpose",
    "ObservationGap",
    "PendingIntent",
    "PositionLedger",
    "Signal",
    "SignalPolicy",
    "SpreadCandidate",
    "SpreadEconomics",
    "SpreadLeg",
    "SpreadProtocol",
    "StudyPhase",
    "bear_contract_ok",
    "choose_candidate",
    "detect_gap",
    "evaluate_signal",
    "executable_price",
    "limit_price",
    "remaining_risk_budget",
    "sma",
    "spread_economics",
    "structure_ok",
]
