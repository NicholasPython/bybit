"""双腿价差套利子系统（BLSAE）。

本包实现 PRD 的「实时执行与离线进化物理隔离」中的实时侧：

* :mod:`bybitbot.spread.states` —— 配对状态机与对冲量计算（纯函数）；
* :mod:`bybitbot.spread.risk` —— 配对级硬风控闸门（纯函数，一票否决）；
* :mod:`bybitbot.spread.execution` —— 双腿原子执行与瘸腿自愈；
* :mod:`bybitbot.spread.models` —— 配对、腿订单与对账日志的持久化模型。

设计约束：
    * 状态机与风控判定都是**纯函数**：不写库、不调交易所，只输出结论与理由，
      与 :mod:`bybitbot.risk.engine` 的口径一致，便于回放与审计；
    * 物理腿的成交事实以交易所为准，本包不另立账本；
    * 离线进化平面（周度寻优、影子盘、灰度）只允许下发**合规参数**，
      不允许直接改本包代码（PRD FR-07）。
"""

from bybitbot.spread.states import (
    ABORTED,
    CLOSED,
    CLOSING,
    ERROR_UNHEDGED,
    HEDGED,
    OPENING,
    PairState,
    TransitionError,
    apply_transition,
    equal_notional_qty,
    log_spread,
    quantize_qty,
    unhedged_notional,
    z_score,
)

__all__ = [
    "ABORTED",
    "CLOSED",
    "CLOSING",
    "ERROR_UNHEDGED",
    "HEDGED",
    "OPENING",
    "PairState",
    "TransitionError",
    "apply_transition",
    "equal_notional_qty",
    "log_spread",
    "quantize_qty",
    "unhedged_notional",
    "z_score",
]
