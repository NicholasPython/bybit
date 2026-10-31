"""运行状态机（§4.9「运行状态及动作约束」）。

原文表格：

===========  ===================  ==============================  ===========================
状态          新开仓               退出与撤单                      恢复条件
===========  ===================  ==============================  ===========================
STARTING /   禁止                仅可信且可审计的 bot 撤单；       完成身份、订单、成交、
RECONCILING                      买卖等待可信持仓                 持仓与预留核对
RUNNING      经风控允许          经对应校验允许                  不适用
PAUSED_OPEN  禁止；撤销 bot 开仓  可信且可审计时继续已有仓位退出；  满足暂停原因的解除规则
             挂单                记录未撤成功订单
EXITING      禁止                对指定 bot 仓位按边界减仓，跟踪   已确认目标完成，或暂停并
                                剩余量                          等待用户处理
STOPPED      禁止                进程不执行；交易所挂单和仓位      重新启动后完整核对，按持久化
                                可能仍存在                      启用状态决定是否恢复
===========  ===================  ==============================  ===========================

关键：**STOPPED 不等于已平仓**（OPS-05）—— 只把「已提交/已撤销/已成交/
仍有剩余仓位」当作真实状态展示。
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    RUNNING = "RUNNING"
    PAUSED_OPEN = "PAUSED_OPEN"
    EXITING = "EXITING"
    STOPPED = "STOPPED"


#: 合法迁移。
_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.STARTING: frozenset({RunState.RECONCILING, RunState.STOPPED, RunState.PAUSED_OPEN}),
    RunState.RECONCILING: frozenset({RunState.RUNNING, RunState.PAUSED_OPEN, RunState.STOPPED}),
    RunState.RUNNING: frozenset(
        {RunState.PAUSED_OPEN, RunState.EXITING, RunState.STOPPED, RunState.RECONCILING}
    ),
    RunState.PAUSED_OPEN: frozenset(
        {RunState.RUNNING, RunState.EXITING, RunState.STOPPED, RunState.RECONCILING}
    ),
    RunState.EXITING: frozenset({RunState.RUNNING, RunState.PAUSED_OPEN, RunState.STOPPED}),
    RunState.STOPPED: frozenset({RunState.STARTING}),
}


class RunStateError(Exception):
    """非法状态迁移。"""


class RunStateMachine:
    """运行状态机。

    状态本身持久化在 ``control_command``/``risk_pause`` 之外 —— 这里
    只维护内存中的当前状态与合法迁移，实际的「持久化启用状态」由
    数据库中的暂停记录与配置版本决定（重启后重新推导）。
    """

    def __init__(self, state: RunState = RunState.STARTING) -> None:
        self._state = state

    @property
    def state(self) -> RunState:
        return self._state

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------

    @property
    def allows_new_exposure(self) -> bool:
        """是否允许新开仓。"""
        return self._state is RunState.RUNNING

    @property
    def allows_exits(self) -> bool:
        """是否允许退出与撤单。

        STARTING / RECONCILING 只允许「可信且可审计的 bot 撤单」，
        买卖要等可信持仓 —— 所以退出动作本身需要额外的持仓可信校验，
        这里只表示「流程上允许进入退出路径」。
        """
        return self._state in {
            RunState.RECONCILING,
            RunState.RUNNING,
            RunState.PAUSED_OPEN,
            RunState.EXITING,
        }

    @property
    def requires_cancel_bot_open_orders(self) -> bool:
        """进入该状态时需要撤销 bot 开仓挂单。"""
        return self._state is RunState.PAUSED_OPEN

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------

    def can_transition(self, target: RunState) -> bool:
        return target in _TRANSITIONS.get(self._state, frozenset())

    def transition(self, target: RunState) -> RunState:
        if not self.can_transition(target):
            raise RunStateError(f"非法状态迁移 {self._state} → {target}")
        self._state = target
        return self._state

    def describe(self) -> str:
        return {
            RunState.STARTING: "启动中：禁止新开仓",
            RunState.RECONCILING: "对账中：禁止新开仓，仅可信 bot 撤单",
            RunState.RUNNING: "运行中：新开仓需经风控允许",
            RunState.PAUSED_OPEN: "已暂停新增风险：撤销 bot 开仓挂单，可信减仓可继续",
            RunState.EXITING: "退出中：按边界减仓指定 bot 仓位",
            RunState.STOPPED: "已停止：进程不执行，交易所挂单与仓位可能仍存在",
        }[self._state]


__all__ = ["RunState", "RunStateError", "RunStateMachine"]
