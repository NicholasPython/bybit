"""崩溃时点、执行权与恢复的组合证据（AC-08/09/12）。

写请求在独立子进程中发生；父进程用 SIGKILL 终止子进程，再用新的执行器
与租约对象恢复。子进程只拿到隔离 schema 的 DSN，不接触真实凭据或网络。

覆盖 requirements.md §4.5 的四个危险时点：
    1. 意图已受理但订单记录未提交；
    2. 写请求已发出但回执未保存；
    3. 交易所已受理但本地 UNKNOWN 未落库；
    4. 成交已发生但本地未入账。
以及 EXE-05 的双进程租约竞争、失权与人工接管前置条件。

跨测试文件复用 test_execution 的辅助件时用顶层导入
``from test_execution import ...``，不要写 ``from tests.test_execution import ...``：
tests/ 没有 ``__init__.py``，pytest 默认的 prepend 导入模式会把 tests/ 挂到
``sys.path`` 上，顶层导入在 ``uv run pytest``（CWD 不在 sys.path）下同样成立；
``tests.`` 包形式只在 ``python -m pytest`` 下能解析，CI 会 ModuleNotFoundError。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import signal
import subprocess
import sys
import textwrap
import uuid
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from bybitbot.clock import now_utc
from bybitbot.db.models import (
    Execution,
    Instrument,
    LedgerEvent,
    Order,
    Reservation,
    RiskPause,
    Strategy,
    TradeIntent,
)
from bybitbot.db.models import (
    ExecutorLease as LeaseRow,
)
from bybitbot.execution.lease import ExecutorLease, LeaseState
from bybitbot.execution.states import SYNC_PENDING, SYNC_UNCERTAIN, UNKNOWN

pytestmark = pytest.mark.skipif(os.name != "posix", reason="需要 POSIX SIGKILL")

ROOT = Path(__file__).resolve().parents[1]


def dsn_of(database) -> str:
    return database._engine.url.render_as_string(hide_password=False)


async def seed(database, *, category="spot", symbol="BTCUSDT", qty="1", price="10"):
    async with database.sessionmaker() as session:
        strategy = Strategy(environment="paper", name="fault-" + uuid.uuid4().hex[:8], kind="test")
        session.add(strategy)
        await session.flush()
        intent = TradeIntent(
            environment="paper",
            account_uid="paper-account",
            strategy_id=strategy.id,
            strategy_version=1,
            signal_id=uuid.uuid4().hex,
            action="SPOT_BUY" if category == "spot" else "OPTION_BUY_OPEN",
            category=category,
            symbol=symbol,
            side="buy",
            qty=D(qty),
            qty_unit="BTC",
            price_limit=D(price),
            spend_limit=D(qty) * D(price),
            currency="USDT",
            expires_at=now_utc() + dt.timedelta(minutes=5),
            status="APPROVED",
        )
        session.add(intent)
        session.add(
            Instrument(
                environment="paper",
                category=category,
                symbol=symbol,
                base_coin="BTC",
                quote_coin="USDT",
                settle_coin="USDT",
                status="Trading",
                tick_size=D(".01"),
                qty_step=D(".001"),
            )
        )
        await session.flush()
        reservation = Reservation(
            environment="paper",
            intent_id=intent.id,
            kind="CASH",
            currency="USDT",
            amount=D(qty) * D(price) + D("1"),
            consumed=D("0"),
            released=D("0"),
            status="HELD",
        )
        session.add(reservation)
        await session.commit()
        return intent.id, reservation.id


def child_script(crash_stage: str) -> str:
    """写请求子进程脚本：在指定时点 SIGKILL 自己。"""
    stage = "before_send" if crash_stage == "before_send" else "after_ack"
    return textwrap.dedent(
        f'''
        import asyncio, os, sys
        from decimal import Decimal as D
        from bybitbot.adapters.base import InstrumentSpec, OrderBook
        from bybitbot.adapters.paper import PaperAdapter
        from bybitbot.db.models import TradeIntent
        from bybitbot.db.session import Database
        from bybitbot.execution.executor import Executor, OrderPlan

        dsn, iid, rid = sys.argv[1:4]
        stage = {stage!r}
        db = Database(dsn)

        class HookedPaper(PaperAdapter):
            async def create_order(self, request):
                if stage == "before_send":
                    print("HOOK before_send", flush=True)
                    os.kill(os.getpid(), 9)
                ack = await super().create_order(request)
                print("HOOK after_ack", flush=True)
                os.kill(os.getpid(), 9)
                return ack

        async def main():
            async with db.sessionmaker() as session:
                intent = await session.get(TradeIntent, int(iid))
                assert intent is not None
                plan = OrderPlan(
                    intent.id, intent.category, intent.symbol, intent.side,
                    qty=intent.qty, price=intent.price_limit,
                    reservation_id=int(rid), spend_limit=intent.spend_limit,
                )
            adapter = HookedPaper(initial_balances={{"USDT": D("1000")}})
            adapter.set_instrument(InstrumentSpec(
                category=intent.category, symbol=intent.symbol, status="Trading",
                tick_size=D(".01"), qty_step=D(".001"),
                base_coin="BTC", quote_coin="USDT", settle_coin="USDT",
            ))
            adapter.set_book(OrderBook(
                category=intent.category, symbol=intent.symbol,
                bids=[(D("9.99"), D("5"))],
                asks=[(D("10"), D("5"))],
            ))
            executor = Executor(
                adapter=adapter,
                sessionmaker=db.sessionmaker, environment="paper",
                account_uid="paper-account",
            )
            await executor.submit(plan)
            await db.dispose()

        asyncio.run(main())
        '''
    )


def run_child(database, crash_stage: str, intent_id: int, reservation_id: int) -> None:
    """在独立的同步子进程里跑到崩溃点。

    用阻塞的 subprocess 而不是 asyncio 子进程：后者依赖事件循环的子进程
    watcher 线程，跨多个事件循环/大量测试后有概率与线程池死锁，把整个
    测试进程挂死（AC-22 之前 CI 曾因此在 verify 步骤超时）。
    """
    script = child_script(crash_stage)
    try:
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-P",
                "-c",
                script,
                dsn_of(database),
                str(intent_id),
                str(reservation_id),
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
        )
        out, err, returncode = proc.stdout.decode(), proc.stderr.decode(), proc.returncode
    except subprocess.TimeoutExpired:
        raise AssertionError(f"崩溃子进程超时：{crash_stage}") from None
    assert f"HOOK {crash_stage}" in out, f"子进程未到达崩溃点：{out}\n{err}"
    assert returncode == -signal.SIGKILL, f"子进程退出码异常：{returncode} {err}"


async def test_CR1_intent_before_prepared_has_no_order_and_releases_reservation(database):
    """崩溃点 1：意图受理但 PREPARED 未持久化。恢复证明未发送。"""
    from test_execution import ControlledPaper, make_executor

    intent_id, reservation_id = await seed(database)
    # 子进程在 submit 之前死亡：进程内没有任何写请求，数据库中只有孤儿预留。
    script = textwrap.dedent(
        '''
        import os, sys
        from bybitbot.db.session import Database
        db = Database(sys.argv[1])
        print("HOOK before_prepare", flush=True)
        os.kill(os.getpid(), 9)
        '''
    )
    try:
        proc = await asyncio.to_thread(  # noqa: S603
            subprocess.run,
            [sys.executable, "-P", "-c", script, dsn_of(database)],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
        )
        out, err, returncode = proc.stdout.decode(), proc.stderr.decode(), proc.returncode
    except subprocess.TimeoutExpired:
        raise AssertionError("子进程超时：before_prepare") from None
    assert "HOOK before_prepare" in out, f"子进程未到达崩溃点：{out}\n{err}"
    assert returncode == -signal.SIGKILL, f"子进程退出码异常：{returncode} {err}"
    async with database.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        reservation = await session.get(Reservation, reservation_id)
        assert reservation.consumed == 0 and reservation.released == 0

    adapter = ControlledPaper(database.sessionmaker)
    reconciler = __import__("bybitbot.reconciliation", fromlist=["Reconciler"]).Reconciler(
        adapter=adapter,
        sessionmaker=database.sessionmaker,
        environment="paper",
        account_uid="paper-account",
        executor=make_executor(database.sessionmaker, adapter),
    )
    result = await reconciler.reconcile_reservations()
    assert result.allows_resume
    async with database.sessionmaker() as session:
        assert (await session.get(Reservation, reservation_id)).released == reservation.amount
    assert adapter.calls == 0


async def test_CR2_send_before_response_is_unknown_no_retry_and_reservation_held(database):
    """崩溃点 2：SUBMITTING/PENDING 时死亡。恢复不得重发或释放预留。"""
    from test_execution import ControlledPaper, make_executor, plan

    intent_id, reservation_id = await seed(database)
    await asyncio.to_thread(
        run_child, database, "before_send", intent_id, reservation_id
    )
    async with database.sessionmaker() as session:
        order = await session.scalar(select(Order).where(Order.intent_id == intent_id))
        assert order.status == "SUBMITTING" and order.sync_state == SYNC_PENDING
        held = await session.get(Reservation, reservation_id)
        assert held.consumed == 0 and held.released == 0

    adapter = ControlledPaper(database.sessionmaker)
    executor = make_executor(database.sessionmaker, adapter)

    class Stub:
        pass

    stub_intent = Stub()
    stub_intent.id, stub_intent.category, stub_intent.symbol = intent_id, "spot", "BTCUSDT"
    stub_intent.side, stub_intent.qty = "buy", D("1")
    stub_intent.price_limit, stub_intent.spend_limit = D("10"), D("10")
    stub_res = Stub()
    stub_res.id = reservation_id
    result = await executor.submit(plan(stub_intent, stub_res))
    assert result.is_unknown
    assert adapter.calls == 0  # 新进程没有发出第二笔写请求
    async with database.sessionmaker() as session:
        held = await session.get(Reservation, reservation_id)
        assert held.released == 0 and held.consumed == 0
        assert await session.scalar(
            select(func.count()).select_from(RiskPause).where(
                RiskPause.reason_code == "UNKNOWN_ORDER", RiskPause.active.is_(True)
            )
        ) == 1


async def test_CR3_exchange_ack_without_local_record_is_reconciled(database):
    """崩溃点 3：交易所已受理，本地未保存。对账用订单事实消解未知。"""
    from test_execution import ControlledPaper, make_executor

    intent_id, reservation_id = await seed(database)
    await asyncio.to_thread(
        run_child, database, "after_ack", intent_id, reservation_id
    )
    async with database.sessionmaker() as session:
        order = await session.scalar(select(Order).where(Order.intent_id == intent_id))
        assert order is not None and order.status in {"SUBMITTING", "ACCEPTED"}

    adapter = ControlledPaper(database.sessionmaker)
    executor = make_executor(database.sessionmaker, adapter)
    from bybitbot.reconciliation import Reconciler

    reconciler = Reconciler(
        adapter=adapter,
        sessionmaker=database.sessionmaker,
        environment="paper",
        account_uid="paper-account",
        executor=executor,
    )
    result = await reconciler.reconcile_orders()
    # 没有交易所挂单事实时必须保存 UNKNOWN，不能悄悄当成功，也不能释放预留。
    assert not result.allows_resume
    assert any(d.kind == "ORDER_NOT_FOUND" for d in result.differences)
    async with database.sessionmaker() as session:
        row = await session.get(Order, order.id)
        assert row.status == UNKNOWN and row.sync_state == SYNC_UNCERTAIN
        assert (await session.get(Reservation, reservation_id)).released == 0


async def test_CR4_fill_before_ledger_replay_is_deduped(database):
    """崩溃点 4：成交已发生但未入账。回放只入账一次，成交数量单调。"""
    from test_execution import ControlledPaper, fill, make_executor, plan, prepared

    adapter = ControlledPaper(database.sessionmaker)
    executor = make_executor(database.sessionmaker, adapter)
    intent, reservation, _ = await prepared(database.sessionmaker)
    accepted = await executor.submit(plan(intent, reservation))
    event = fill(accepted.order, identifier="crash-" + uuid.uuid4().hex)
    async with database.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Execution)) == 0
    assert await executor.apply_execution(event)
    assert not await executor.apply_execution(event)
    async with database.sessionmaker() as session:
        row = await session.get(Order, accepted.order.id)
        assert row.qty_filled == D("1")
        assert await session.scalar(select(func.count()).select_from(Execution)) == 1
        assert await session.scalar(
            select(func.count()).select_from(LedgerEvent)
        ) >= 1


async def test_two_processes_compete_for_lease_and_manual_confirm_is_required(database):
    """AC-12：双进程抢租约只有一个成功；断连失权；人工确认前不能发单。"""
    from test_execution import ControlledPaper, make_executor

    from bybitbot.execution.executor import ExecutionAuthorityError

    uid = "dual-" + uuid.uuid4().hex
    script = textwrap.dedent(
        '''
        import asyncio, sys
        from bybitbot.db.session import Database
        from bybitbot.execution.lease import ExecutorLease
        db = Database(sys.argv[1])
        async def main():
            lease = ExecutorLease(db.engine, environment="paper", account_uid=sys.argv[2])
            status = await lease.try_acquire()
            print(status.state, flush=True)
            if status.state.value == "NEEDS_CONFIRMATION":
                await asyncio.Event().wait()
        asyncio.run(main())
        '''
    )
    # 两个同步子进程抢租约：一个拿到（打印后挂住等 SIGKILL），一个被拒。
    # 子进程管理走阻塞 subprocess + to_thread：不创建 asyncio 子进程 watcher
    # 线程，避免与 pytest-asyncio 的多轮事件循环交互时把测试进程挂死。
    def spawn(index: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603
            [sys.executable, "-P", "-c", script, dsn_of(database), uid],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    procs = [spawn(0), spawn(1)]
    try:
        def read_state(proc: subprocess.Popen[bytes]) -> str:
            assert proc.stdout is not None
            line = proc.stdout.readline().decode().strip()
            assert line in {"NEEDS_CONFIRMATION", "HELD_BY_OTHER", "LOST"}, line
            return line

        states = list(await asyncio.gather(*(
            asyncio.to_thread(read_state, proc) for proc in procs
        )))
        assert sorted(states) == ["HELD_BY_OTHER", "NEEDS_CONFIRMATION"]
        async with database.sessionmaker() as session:
            held = list(await session.scalars(
                select(LeaseRow).where(
                    LeaseRow.account_uid == uid, LeaseRow.released_at.is_(None)
                )
            ))
            assert len(held) == 1
        winner = next(p for p, s in zip(procs, states, strict=True) if s == "NEEDS_CONFIRMATION")
        winner.kill()
        await asyncio.to_thread(winner.communicate)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            await asyncio.to_thread(proc.communicate)

    lease = ExecutorLease(database.engine, environment="paper", account_uid=uid)
    assert (await lease.try_acquire()).state == LeaseState.NEEDS_CONFIRMATION
    executor = make_executor(database.sessionmaker, ControlledPaper(database.sessionmaker))
    executor.lease = lease
    # 纸面执行器的 account_uid 是 paper-account，而租约属于自定义 uid：
    # 租约与执行器身份不匹配时同样拒绝发单（EXE-05 的身份门槛）。
    with pytest.raises(ExecutionAuthorityError, match="租约账户或环境不匹配"):
        await executor._require_authority()
    # 空证据不能确认接管。
    with pytest.raises(RuntimeError, match="接管需要操作者与核对证据"):
        await lease.confirm_takeover(operator="operator", evidence="  ")
    assert (await lease.confirm_takeover(
        operator="operator", evidence="old process SIGKILLed and reconciled"
    )).state == LeaseState.ELIGIBLE
    assert (await lease.heartbeat()).state == LeaseState.ELIGIBLE
    await lease.release()


async def test_database_disconnect_invalidates_authority_immediately(database):
    """数据库断连 → 会话级锁释放 → 心跳失权；已发写请求不重试。"""
    from test_execution import ControlledPaper, make_executor, plan, prepared

    from bybitbot.execution.executor import ExecutionAuthorityError

    intent, reservation, _ = await prepared(database.sessionmaker)
    adapter = ControlledPaper(database.sessionmaker)
    executor = make_executor(database.sessionmaker, adapter)
    lease = ExecutorLease(database.engine, environment="paper", account_uid="paper-account")
    assert (await lease.try_acquire()).state == LeaseState.NEEDS_CONFIRMATION
    await lease.confirm_takeover(operator="test", evidence="isolated schema")
    executor.lease = lease
    result = await executor.submit(plan(intent, reservation))
    assert result.outcome.value == "ACCEPTED"
    async with lease._connection.begin_nested():
        await lease._connection.execute(text("SELECT pg_advisory_unlock_all()"))
    assert (await lease.heartbeat()).state == LeaseState.LOST
    with pytest.raises(ExecutionAuthorityError, match="执行资格"):
        await executor.submit(plan(intent, reservation))
    assert adapter.calls == 1
    await lease.release()
