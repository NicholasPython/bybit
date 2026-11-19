"""风控组合场景（AC-07/14/27）：双策略、双侧 Delta、负债与减仓路径。

这些用例不是重复已有单策略检查，而是把需求评审里点名的组合缺口变成断言：
    * 双策略账户预算与策略子额度同时约束；
    * 大负 Delta 与相反挂单不能互相抵消；
    * 运行中新增负债必须暂停新增风险但不阻塞可信减仓；
    * 结算币现金不足时不能用抵押品或浮盈代替。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from bybitbot.adapters.base import AccountView, CoinBalance, Ticker
from bybitbot.clock import now_utc
from bybitbot.db.models import TradeIntent
from bybitbot.risk.engine import OpenOrderExposure, RiskContext, RiskEngine
from bybitbot.risk.reservation import ReservationError, ReservationManager
from bybitbot.settings.loader import load_config


def account(*, cash="1000", available=None, borrowed="0", interest="0", equity=None):
    coin = CoinBalance(
        "USDT",
        D(equity or cash),
        D(available or cash),
        D(cash),
        borrowed=D(borrowed),
        accrued_interest=D(interest),
    )
    return AccountView("paper-account", "unified", {"USDT": coin}, raw={})


def intent(**kwargs):
    values = {
        "environment": "paper",
        "account_uid": "paper-account",
        "signal_id": "risk-combo",
        "action": "SPOT_BUY",
        "category": "spot",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": D(".1"),
        "qty_unit": "BTC",
        "price_limit": D("100"),
        "spend_limit": D("10"),
        "currency": "USDT",
        "expires_at": now_utc() + dt.timedelta(minutes=1),
        "status": "APPROVED",
    }
    values.update(kwargs)
    return TradeIntent(**values)


def ctx(**kwargs):
    moment = now_utc()
    quote = Ticker(
        "spot",
        "BTCUSDT",
        bid_price=D("99.9"),
        ask_price=D("100"),
        source_ts=moment,
        received_at=moment,
    )
    values = {
        "account": account(),
        "positions": [],
        "quotes": {"BTCUSDT": quote},
        "open_orders": [],
        "identity_trusted": True,
        "valuation_trusted": True,
        "account_delta_per_underlying": {"BTC": D("0")},
        "risk_equity": D("1000"),
    }
    values.update(kwargs)
    return RiskContext(**values)


def config():
    return load_config("config/config.example.yaml")


async def test_account_and_strategy_budgets_both_bind_in_sequence():
    """双策略共享账户预算：逐笔收紧后，账户级与策略级限额都拒绝。"""
    engine = RiskEngine(config())
    # 账户级现货敞口 500，策略级 200；当前已用 195，两个新意图各 10。
    first_ctx = ctx(account_spot_exposure=D("190"), strategy_spot_exposure=D("190"))
    first = await engine.evaluate(intent(), first_ctx)
    assert first.approved
    # 账户总额度只差 5：即使该策略子额度还有余量，账户级约束也拒绝。
    second_ctx = ctx(account_spot_exposure=D("495"), strategy_spot_exposure=D("20"))
    second = await engine.evaluate(intent(spend_limit=D("10")), second_ctx)
    assert not second.approved
    assert any(
        item.code == "ACCOUNT_EXPOSURE_LIMIT" and not item.approved
        for item in second.checks
    )
    # 账户还有额度但该策略已达 200：策略子额度单独拒绝。
    third_ctx = ctx(account_spot_exposure=D("100"), strategy_spot_exposure=D("200"))
    third = await engine.evaluate(intent(), third_ctx)
    assert not third.approved
    assert any(
        item.code == "STRATEGY_EXPOSURE_LIMIT" and not item.approved for item in third.checks
    )


async def test_large_negative_delta_and_opposite_orders_do_not_net_off():
    """AC-14：大负 Delta 与方向相反的挂单按保守区间，不相互抵消。"""
    engine = RiskEngine(config())
    large_negative = ctx(
        account_delta_per_underlying={"BTC": D("-2")},
        open_orders=[OpenOrderExposure(1, "BTCUSDT", "buy", D(".1"), D(".1"))],
    )
    check = await engine.evaluate(intent(), large_negative)
    assert not check.approved
    assert any(c.code == "DELTA_BOUNDS" and not c.approved for c in check.checks)
    opposite = ctx(
        account_delta_per_underlying={"BTC": D("0")},
        open_orders=[
            OpenOrderExposure(1, "BTCUSDT", "buy", D("2"), D("2")),
            OpenOrderExposure(2, "BTCUSDT", "sell", D("2"), D("-2")),
        ],
    )
    check = await engine.evaluate(intent(), opposite)
    assert not check.approved
    assert any(c.code == "DELTA_BOUNDS" and not c.approved for c in check.checks)


async def test_new_debt_pauses_new_exposure_but_not_trusted_reduce():
    """AC-27：新增负债触发账户暂停，但可信减仓仍单独判定。"""
    engine = RiskEngine(config())
    debt = account(borrowed="1")
    opening = await engine.evaluate(intent(), ctx(account=debt))
    assert not opening.approved
    assert any(
        c.code in {"ACCOUNT_DEBT", "ACCOUNT_PAUSE"} and not c.approved
        for c in opening.checks
    )
    reduce_intent = intent(
        action="SPOT_SELL", side="sell", price_limit=D("99.9"), spend_limit=D("0")
    )
    reduce_ctx = ctx(
        account=debt,
        daily_pnl=D("999"),
        drawdown=D("999"),
        account_pause_reasons=["ACCOUNT_DEBT"],
        open_orders=[],
    )
    closing = await engine.evaluate(reduce_intent, reduce_ctx)
    assert closing.approved


async def test_settlement_cash_shortage_cannot_be_replaced_by_collateral(sessionmaker):
    """RSK-02：结算币现金不足时拒绝；抵押品/浮盈不当作可支出现金。"""
    manager = ReservationManager("paper")
    # 账户只有 5 USDT 可用，但抵押品/权益估值很高。意图需要 10 USDT。
    view = account(cash="500", available="5", equity="500")
    async with sessionmaker() as session:
        with pytest.raises(ReservationError) as caught:
            await manager.reserve_cash(
                session,
                intent_id=1,
                amount=D("10"),
                currency="USDT",
                account=view,
            )
        assert caught.value.code == "INSUFFICIENT_CASH"
        assert "自有现金 500" in caught.value.detail
        assert "可用 5" in caught.value.detail
