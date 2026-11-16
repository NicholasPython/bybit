"""可重复的本地 paper 系统入口：独立 schema、合成行情、完整账本验收。

不读取交易凭据、不请求公共行情、不更改默认配置或已有数据库表。
沙盒生成的管理员身份和数据库配置仅写入权限受限的本地目录。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import uuid
from decimal import Decimal as D
from pathlib import Path

import yaml
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from bybitbot.clock import now_utc
from bybitbot.db.models import (
    ControlCommand,
    Execution,
    Inventory,
    Order,
    RiskDecision,
    TradeIntent,
)
from bybitbot.db.session import Database
from bybitbot.execution.lease import LeaseState
from bybitbot.ops.control import CommandKind, CommandStatus
from bybitbot.service import TradingService
from bybitbot.settings.loader import load_config
from bybitbot.strategy.base import IntentDraft


def _no_overrides():
    if os.environ.get("BYBITBOT_DB_DSN") or os.environ.get("BYBITBOT_ENV"):
        raise RuntimeError("沙盒禁止 BYBITBOT_DB_DSN/BYBITBOT_ENV 覆盖隔离配置，请移除后运行")


def _write_private(path: Path, content: str):
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write(content)


def _migrate(dsn: str):
    from alembic.config import Config

    from alembic import command

    config = Config("alembic.ini")
    config.attributes["db_dsn"] = dsn
    command.upgrade(config, "head")


async def initialize(directory: Path, *, dsn: str, template: Path):
    """只接受本机 PostgreSQL，新建随机命名 schema，不覆盖已有目录。"""
    _no_overrides()
    url = make_url(dsn)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.query
    ):
        raise ValueError("沙盒 DSN 必须是本机 postgresql+psycopg 连接，不得带查询参数")
    config = load_config(template, env_override="paper")
    token = uuid.uuid4().hex
    schema = "sandbox_" + token
    scoped_dsn = url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
        hide_password=False
    )
    expiry = (now_utc() + dt.timedelta(days=14)).replace(hour=8, minute=0, second=0, microsecond=0)
    raw = config.model_dump(mode="json")
    raw["environments"] = {
        "paper": {
            "environment": "paper",
            "display_name": "本地沙盒 · 合成行情",
            "db_dsn": scoped_dsn,
            "log_prefix": schema,
            "allow_credentials": False,
            "allow_trade_writes": False,
        }
    }
    raw["account"]["expected_uid"] = schema
    raw["paper"].update(market_source="synthetic", synthetic_expiry=expiry.isoformat())
    raw["runtime"].update(proxy=None, poll_seconds=2)
    raw["live_gates"] = {"live_allowed": False}
    raw["ops"].update(alert_channels=[], risk_start_at=None)
    # 固定合成合约没有真实交易所日切窗口；仍保留到期、预算和持仓风控。
    raw["option"].update(no_open_window_start_hour_utc=0, no_open_window_end_hour_utc=0)
    # 每天一次，初始时刻在一小时后；保留模板各策略原有冷却时间。
    scheduled = (now_utc() + dt.timedelta(hours=1)).strftime("%H:%M")
    for strategy in raw["strategies"]:
        strategy.update(enabled=True, schedule_utc=[scheduled])
    directory = await asyncio.to_thread(directory.resolve)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    config_path = directory / "config.yaml"
    _write_private(config_path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    load_config(config_path)  # 在建 schema 之前完成最终配置校验。
    engine = create_async_engine(dsn, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()
    await asyncio.to_thread(_migrate, scoped_dsn)
    from bybitbot.cli import init_admin

    init_admin(directory / "admin.json", password_file=directory / "admin-password.txt")
    _write_private(
        directory / "sandbox.json",
        json.dumps(
            {
                "format": "sandbox-v1",
                "schema": schema,
                "account_uid": schema,
                "created_at": now_utc().isoformat(),
                "market_source": "synthetic",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return {
        "directory": str(directory),
        "schema": schema,
        "environment": "paper",
        "market_source": "synthetic",
        "live_allowed": False,
    }


def load_sandbox(directory: Path):
    _no_overrides()
    metadata = json.loads((directory / "sandbox.json").read_text())
    schema = metadata["schema"]
    if metadata.get("format") != "sandbox-v1" or not re.fullmatch(r"sandbox_[a-f0-9]{32}", schema):
        raise ValueError("不是有效的独立沙盒")
    config = load_config(directory / "config.yaml")
    url = make_url(config.profile.db_dsn)
    if (
        config.active_environment.value != "paper"
        or {str(key) for key in config.environments} != {"paper"}
        or config.paper.market_source != "synthetic"
        or config.live_gates.live_allowed
        or config.account.expected_uid != schema
        or metadata.get("account_uid") != schema
        or url.drivername != "postgresql+psycopg"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or dict(url.query) != {"options": f"-csearch_path={schema}"}
    ):
        raise ValueError("沙盒的环境、行情或数据库隔离配置已变更，拒绝自动验收")
    return config


async def _command(service, kind, payload):
    row, _ = await service.commands.submit(
        command_id=str(uuid.uuid4()), kind=kind, payload=payload, requested_by="sandbox-acceptance"
    )
    await service._process_commands()
    async with service.database.sessionmaker() as session:
        row = await session.get(ControlCommand, row.id)
    if row.status != CommandStatus.EXECUTED:
        raise RuntimeError(f"沙盒控制命令未成功：{kind} {row.status} {row.result}")
    return row.result


async def _facts(database):
    async with database.sessionmaker() as session:
        orders = list(await session.scalars(select(Order).order_by(Order.id)))
        fills = list(await session.scalars(select(Execution).order_by(Execution.id)))
        inventory = list(await session.scalars(select(Inventory).order_by(Inventory.id)))
        return {
            "orders": [
                {
                    "id": row.order_link_id,
                    "symbol": row.symbol,
                    "status": row.status,
                    "side": row.side,
                    "qty": str(row.qty),
                    "filled": str(row.qty_filled),
                }
                for row in orders
            ],
            "executions": [
                {
                    "id": row.exchange_exec_id,
                    "symbol": row.symbol,
                    "qty": str(row.exec_qty),
                    "fee": str(row.exec_fee),
                    "fee_coin": row.exec_fee_asset,
                }
                for row in fills
            ],
            "inventory": [
                {
                    "strategy_id": row.strategy_id,
                    "symbol": row.symbol,
                    "qty": str(row.qty),
                    "trusted": row.trusted,
                }
                for row in inventory
            ],
        }


def _check(condition, detail):
    if not condition:
        raise RuntimeError(f"沙盒验收失败：{detail}")


async def verify(directory: Path):
    """通过正常策略/控制/风控/执行/账本链路验收，只接受未交易的沙盒。"""
    config = load_sandbox(directory)
    # 验收显式投递调度任务，避免与日历任务同时运行而额外开仓。
    moment = now_utc()
    for strategy in config.strategies:
        for slot in strategy.schedule_utc:
            hour, minute = map(int, slot.split(":"))
            planned = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
            distance = abs((moment - planned).total_seconds())
            if min(distance, 86400 - distance) < 120:
                raise RuntimeError("验收时刻与日历调度相邻，请在两分钟后验收或创建新沙盒")
    database = Database(config.profile.db_dsn)
    service = TradingService(config, database)
    report_path = directory / "verification.json"
    if report_path.exists():
        await database.dispose()
        raise RuntimeError("已有验收记录，拒绝重复交易；请创建新沙盒")
    checks = []
    started = False
    try:
        # 先检查，避免已有沙盒启动时撮合在途订单。
        async with database.sessionmaker() as session:
            count = await session.scalar(select(func.count()).select_from(TradeIntent))
        _check(count == 0, "只允许未交易的独立沙盒")
        await service.start()
        started = True
        _check(service.lease.state is LeaseState.NEEDS_CONFIRMATION, "首次启动须确认接管")
        _check(service.machine.state.value == "PAUSED_OPEN", "首次启动暂停新增风险")
        await _command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "本地合成系统验收"})
        await _command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {
                "reason": "本地合成系统验收",
                "evidence": "全新独立 schema，当前进程已取得唯一执行锁",
            },
        )
        await service.tick()
        _check(service.machine.allows_new_exposure and service.baseline_ok, "基线和执行资格就绪")
        checks.append("startup_baseline_takeover")
        for strategy_id, (strategy, version) in service.strategies.items():
            await service.scheduler.plan(
                strategy_id=strategy_id,
                strategy_version=version,
                rule_id=f"sandbox-acceptance:{strategy.name}",
                planned_at=now_utc(),
                input_snapshot={"symbol": strategy.symbols[0]},
            )
        await service._schedule()
        submitted = await _facts(database)
        _check(len(submitted["orders"]) == len(config.strategies), "两种策略均生成受理订单")
        _check(not submitted["executions"], "下单前的旧盘口不能产生成交")
        await service.tick()
        opened = await _facts(database)
        _check(all(row["status"] == "FILLED" for row in opened["orders"]), "新盘口撮合全部订单")
        _check(len(opened["executions"]) == len(config.strategies), "每笔成交仅入账一次")
        _check(all(D(row["fee"]) > 0 for row in opened["executions"]), "费用按版本模型入账")
        checks.extend(["scheduled_spot_and_option", "next_quote_fill", "fees_and_owned_inventory"])
        await _command(service, CommandKind.PAUSE_NEW_RISK, {"reason": "暂停及减仓验收"})
        await service.tick()
        spot_id, (_, version) = next(
            (sid, pair)
            for sid, pair in service.strategies.items()
            if pair[0].kind == "spot_fixed_budget_dca"
        )
        draft = IntentDraft(
            action="SPOT_BUY",
            category="spot",
            symbol="BTCUSDT",
            side="Buy",
            qty=D("0.0001"),
            qty_unit="BTC",
            currency="USDT",
            spend_limit=D("11"),
            price_limit=D("100000"),
            ttl_seconds=60,
        )
        rejected = await service._submit_draft(
            spot_id, version, draft, planned_at=now_utc(), signal="sandbox-paused-open"
        )
        _check(rejected["status"] == "REJECTED", "暂停后禁止新增风险")
        async with database.sessionmaker() as session:
            decision = await session.scalar(
                select(RiskDecision).where(RiskDecision.intent_id == rejected["intent_id"])
            )
        _check(
            any(
                check["code"] == "ACCOUNT_PAUSED" and not check["approved"]
                for check in decision.checks
            ),
            "活动暂停的风险检查明确拒绝",
        )
        checks.append("pause_rejects_new_risk")
        option = next(row for row in opened["inventory"] if "-" in row["symbol"])
        quote = await service.market_adapter.get_ticker("option", option["symbol"])
        result = await _command(
            service,
            CommandKind.REDUCE,
            {
                "strategy_id": option["strategy_id"],
                "category": "option",
                "symbol": option["symbol"],
                "qty": option["qty"],
                "price_floor": str(quote.bid_price),
                "reason": "暂停中可信库存减仓",
            },
        )
        _check(result["status"] == "ACCEPTED", "暂停允许可信减仓")
        await service.tick()
        reduced = await _facts(database)
        _check(len(reduced["executions"]) == len(opened["executions"]) + 1, "减仓成交已入账")
        _check(
            D(next(row for row in reduced["inventory"] if row["symbol"] == option["symbol"])["qty"])
            == 0,
            "期权库存已平仓",
        )
        checks.append("reduce_while_paused")
        checkpoint = await service.store.get("paper")
        await service.close()
        started = False
        service = TradingService(config, database)
        await service.start()
        started = True
        restored = await service.store.get("paper")
        _check(restored["coins"] == checkpoint["coins"], "重启不重置模拟余额")
        _check(await _facts(database) == reduced, "重启不重放成交或增添库存")
        _check(service.lease.state is LeaseState.NEEDS_CONFIRMATION, "重启重新要求接管确认")
        _check(bool(await service.pauses.active_pauses()), "人工暂停跨重启保留")
        checks.extend(["restart_preserves_balances_and_fills", "restart_requires_takeover"])
        report = {
            "format": "paper-system-acceptance-v1",
            "at": now_utc().isoformat(),
            "environment": "paper",
            "market_source": "synthetic",
            "real_fills_verified": False,
            "fee_version": config.fees.version,
            "valuation_version": config.ops.valuation.version,
            "checks": checks,
            "passed": True,
            **reduced,
            "limitations": "离线合成价格；不刻画真实排队、延迟、市场冲击或交易所权限。",
        }
        _write_private(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report
    finally:
        if started:
            await service.close()
        await database.dispose()
