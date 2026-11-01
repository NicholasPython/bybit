"""持久化控制队列（OPS-02 / SEC-02）。Web 只入队，唯一服务领取执行。

EXECUTING 状态跨崩溃保留，不能因没有写入结果就重做交易写请求。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from bybitbot.clock import now_utc
from bybitbot.db.models import ControlCommand, Order, TradeIntent
from bybitbot.ops.pauses import PauseManager, PauseReason


class CommandKind(StrEnum):
    PAUSE_NEW_RISK = "PAUSE_NEW_RISK"
    CANCEL_BOT_OPEN_ORDERS = "CANCEL_BOT_OPEN_ORDERS"
    CANCEL_ORDER = "CANCEL_ORDER"
    REDUCE = "REDUCE"
    RESUME = "RESUME"
    CONFIRM_TAKEOVER = "CONFIRM_TAKEOVER"
    INITIALIZE_BASELINE = "INITIALIZE_BASELINE"
    #: 登记外部充值/提现/划转（LED-03 / LED-04）。
    REGISTER_EXTERNAL_CASH_FLOW = "REGISTER_EXTERNAL_CASH_FLOW"


class CommandStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


@dataclass
class CommandView:
    id: int
    command_id: str
    kind: str
    payload: dict[str, Any]
    status: str
    requested_by: str
    result: dict[str, Any]
    created_at: dt.datetime
    executed_at: dt.datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "created_at": self.created_at.isoformat(),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }


class ControlCommandManager:
    def __init__(self, sessionmaker, environment: str, *, pauses: PauseManager | None = None):
        self.sessionmaker = sessionmaker
        self.environment = environment
        self.pauses = pauses or PauseManager(sessionmaker, environment)

    async def submit(
        self, *, command_id: str, kind: str, payload: dict[str, Any], requested_by: str
    ) -> tuple[ControlCommand, bool]:
        CommandKind(kind)
        async with self.sessionmaker() as session:
            row_id = await session.scalar(
                insert(ControlCommand)
                .values(
                    environment=self.environment,
                    command_id=command_id,
                    kind=kind,
                    payload=payload,
                    status=CommandStatus.PENDING,
                    requested_by=requested_by,
                    result={},
                    created_at=now_utc(),
                )
                .on_conflict_do_nothing(constraint="uq_control_command_id")
                .returning(ControlCommand.id)
            )
            row = await session.scalar(
                select(ControlCommand).where(
                    ControlCommand.environment == self.environment,
                    ControlCommand.command_id == command_id,
                )
            )
            if row.kind != kind or row.payload != payload:
                raise ValueError("命令编号已经用于不同的操作")
            await session.commit()
            return row, row_id is not None

    async def execute(
        self,
        command: ControlCommand,
        *,
        executor=None,
        operator: str | None = None,
        resume_handler=None,
        reduce_handler=None,
        takeover_handler=None,
        baseline_handler=None,
        cash_flow_handler=None,
    ):
        async with self.sessionmaker() as session:
            claimed = await session.scalar(
                update(ControlCommand)
                .where(
                    ControlCommand.id == command.id,
                    ControlCommand.environment == self.environment,
                    ControlCommand.status == CommandStatus.PENDING,
                )
                .values(status=CommandStatus.EXECUTING)
                .returning(ControlCommand.id)
            )
            await session.commit()
        if claimed is None:
            async with self.sessionmaker() as session:
                return await session.get(ControlCommand, command.id)
        try:
            result = await self._dispatch(
                command,
                executor=executor,
                operator=operator or command.requested_by,
                resume_handler=resume_handler,
                reduce_handler=reduce_handler,
                takeover_handler=takeover_handler,
                baseline_handler=baseline_handler,
                cash_flow_handler=cash_flow_handler,
            )
            status = CommandStatus.EXECUTED
        except (ValueError, RuntimeError) as exc:
            result, status = {"error": str(exc)}, CommandStatus.REJECTED
        except Exception as exc:
            result, status = (
                {"error": type(exc).__name__, "detail": "执行结果待核实，请查看订单与对账记录"},
                CommandStatus.FAILED,
            )
        async with self.sessionmaker() as session:
            row = await session.get(ControlCommand, command.id)
            row.status, row.result, row.executed_at = status, result, now_utc()
            await session.commit()
            return row

    async def _dispatch(
        self,
        command,
        *,
        executor,
        operator,
        resume_handler,
        reduce_handler,
        takeover_handler,
        baseline_handler,
        cash_flow_handler,
    ):
        payload = command.payload or {}
        if command.kind == CommandKind.PAUSE_NEW_RISK:
            pause = await self.pauses.pause(
                reason_code=PauseReason.MANUAL,
                reason_detail=payload.get("reason", "人工暂停"),
                locked=True,
                evidence={"operator": operator, "command_id": command.command_id},
            )
            return {"paused": True, "pause_id": pause.id, "scope": "ACCOUNT"}
        handlers = {
            CommandKind.RESUME: resume_handler,
            CommandKind.REDUCE: reduce_handler,
            CommandKind.CONFIRM_TAKEOVER: takeover_handler,
            CommandKind.INITIALIZE_BASELINE: baseline_handler,
            CommandKind.REGISTER_EXTERNAL_CASH_FLOW: cash_flow_handler,
        }
        if command.kind in handlers:
            handler = handlers[command.kind]
            if handler is None:
                raise RuntimeError("执行服务尚未具备该操作的校验能力")
            return await handler(command)
        if command.kind in {CommandKind.CANCEL_ORDER, CommandKind.CANCEL_BOT_OPEN_ORDERS}:
            if executor is None:
                raise RuntimeError("没有可用执行器")
            targets = await self._resolve_cancel_targets(command)
            confirmed, pending = [], []
            for order in targets:
                result = await executor.cancel(order)
                if result.resolved:
                    confirmed.append(order.order_link_id)
                else:
                    pending.append({"order_link_id": order.order_link_id, "detail": result.detail})
            return {"terminal_confirmed": confirmed, "pending": pending, "scope": command.kind}
        raise ValueError("未知命令类型")

    async def _resolve_cancel_targets(self, command) -> list[Order]:
        payload = command.payload or {}
        statement = (
            select(Order)
            .join(TradeIntent, TradeIntent.id == Order.intent_id)
            .where(
                Order.environment == self.environment,
                Order.status.notin_(["FILLED", "CANCELED", "REJECTED", "EXPIRED"]),
            )
        )
        if command.kind == CommandKind.CANCEL_ORDER:
            statement = statement.where(Order.order_link_id.in_(payload.get("order_link_ids", [])))
        else:
            statement = statement.where(
                TradeIntent.action.in_(["BUY_OPEN", "SPOT_BUY", "OPTION_BUY_OPEN"])
            )
        if payload.get("symbol"):
            statement = statement.where(Order.symbol == payload["symbol"])
        if payload.get("strategy_id"):
            statement = statement.where(TradeIntent.strategy_id == payload["strategy_id"])
        async with self.sessionmaker() as session:
            return list(await session.scalars(statement))

    async def pending(self):
        async with self.sessionmaker() as session:
            return list(
                await session.scalars(
                    select(ControlCommand)
                    .where(
                        ControlCommand.environment == self.environment,
                        ControlCommand.status == CommandStatus.PENDING,
                    )
                    .order_by(ControlCommand.id)
                    .limit(20)
                )
            )

    async def recent(self, limit: int = 50) -> list[CommandView]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(ControlCommand)
                .where(
                    ControlCommand.environment == self.environment,
                )
                .order_by(ControlCommand.created_at.desc())
                .limit(limit)
            )
            return [
                CommandView(**{key: getattr(row, key) for key in CommandView.__dataclass_fields__})
                for row in rows
            ]
