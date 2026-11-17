"""外部资金变化的对账闭环（AC-16 / LED-03）。

未登记的外部现金变化被判为 CRITICAL 差异；由控制命令登记后，带交易所流水
标识的资金流让同一变化可被解释。审计事件与资金流按去重键防止重复登记。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal as D

from sqlalchemy import func, select

from bybitbot.adapters.base import AccountView, CoinBalance
from bybitbot.clock import now_utc
from bybitbot.db.models import AccountSnapshot, CashFlow, LedgerEvent, ReconciliationRun
from bybitbot.reconciliation import Reconciler


class MutableAccount:
    """只读账户视图，现金可由测试改变以模拟账户外划转。"""

    def __init__(self, balance: D):
        self.balance = balance

    async def get_account(self) -> AccountView:
        return AccountView(
            account_uid="paper-account",
            account_mode="unified",
            coins={
                "USDT": CoinBalance(
                    "USDT", self.balance, self.balance, self.balance
                )
            },
        )


def reconciler(sessionmaker, adapter):
    return Reconciler(
        adapter=adapter,
        sessionmaker=sessionmaker,
        environment="paper",
        account_uid="paper-account",
        executor=None,
    )


async def snapshot(sessionmaker, balance: D):
    async with sessionmaker() as session:
        session.add(
            AccountSnapshot(
                environment="paper",
                account_uid="paper-account",
                account_mode="unified",
                coins={"USDT": {"wallet_balance": str(balance)}},
                complete=True,
                captured_at=now_utc() - dt.timedelta(seconds=1),
            )
        )
        await session.commit()


async def test_external_cash_change_requires_registration_before_resume(sessionmaker):
    """未登记 → CRITICAL 且不允许恢复；登记后现金变化可解释。"""

    adapter = MutableAccount(D("1000"))
    await snapshot(sessionmaker, D("1000"))
    adapter.balance = D("1100")

    before = await reconciler(sessionmaker, adapter).reconcile_cash()
    assert not before.allows_resume
    assert any(d.kind == "EXTERNAL_CASH_CHANGE" for d in before.differences)

    # 由人工命令登记交易所流水（服务层要求非 paper 环境，这里直接调用账本写入，
    # 验证对账口径：去重键、外部标记和审计事件构成完整闭环）。
    from bybitbot.ledger import LedgerWriter

    ledger = LedgerWriter("paper")
    reference = "bybit-transfer-" + uuid.uuid4().hex[:8]
    async with sessionmaker() as session:
        flow = await ledger.record_cash_flow(
            session,
            asset="USDT",
            amount=D("100"),
            kind="DEPOSIT",
            dedupe_key=f"external-cash-flow:{reference}",
            occurred_at=now_utc(),
            external=True,
        )
        assert flow is not None
        await ledger.append_event(
            session,
            event_type="REGISTER_EXTERNAL_CASH_FLOW",
            dedupe_key=f"audit:external-cash-flow:{uuid.uuid4().hex}",
            payload={
                "category": "cash_flow",
                "operator": "test-operator",
                "cash_flow_id": flow.id,
                "reference": reference,
                "amount": "100",
            },
        )
        await session.commit()

    after = await reconciler(sessionmaker, adapter).reconcile_cash()
    assert after.allows_resume
    assert not any(d.kind == "EXTERNAL_CASH_CHANGE" for d in after.differences)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(CashFlow)) == 1
        assert await session.scalar(
            select(func.count()).select_from(LedgerEvent).where(
                LedgerEvent.event_type == "REGISTER_EXTERNAL_CASH_FLOW"
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(ReconciliationRun)
        ) == 2
