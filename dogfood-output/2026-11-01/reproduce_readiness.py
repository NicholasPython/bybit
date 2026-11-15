"""Local-only business-logic reproductions; never creates a remote adapter or DB.

Run from repository root:
    .venv/bin/python dogfood-output/2026-11-01/reproduce_readiness.py

This script was written against the code as of 2026-11-01 to reproduce the
findings in report.md (CR-01, CR-02). After the remediation it now demonstrates
the **fixed** behaviour:

* CR-01 — a fully closed historical batch no longer triggers the exit of a new
  inventory round. The pre-fix expectation (``exit_submitted_to_mock: true``)
  is recorded in report.md and code-review.md.
* CR-02 — the paper adapter charges the official option fee formula
  ``min(index × 0.03%, premium × 7%) × qty``; the pre-fix value was 0.15 USDT.

The exit reproduction supplies in-memory query results and intercepts
submission. The fee reproduction calls PaperAdapter's in-memory accounting only.
"""

import asyncio
import datetime as dt
import json
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bybitbot.adapters.base import InstrumentSpec, OrderRequest, Ticker
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import now_utc
from bybitbot.service import TradingService
from bybitbot.settings.loader import load_config
from bybitbot.settings.models import OrderSide, OrderType


async def reproduce_old_exit_rule():
    """同策略同合约：旧批次已平仓，新批次刚买入，持有期未满不应退出。"""
    now = now_utc()
    rule = {
        "kind": "hold_period", "hold_seconds": 86400,
        "max_reprice_attempts": 1, "exit_validity_seconds": 300,
    }
    # 第一批：两天前买入，昨天平仓（退出意图 id=2）。
    old = NS(id=1, strategy_id=1, symbol="BTCUSDT", strategy_version="v1",
             exit_rules=[rule], qty_unit="BTC", underlying="BTC",
             created_at=now - dt.timedelta(days=2))
    # 第二批：刚刚买入，构成新的库存持有周期。
    new = NS(id=3, strategy_id=1, symbol="BTCUSDT", strategy_version="v1",
             exit_rules=[rule], qty_unit="BTC", underlying="BTC", created_at=now)
    inventory = NS(strategy_id=1, symbol="BTCUSDT", qty=D("1"),
                   avg_cost=D("100"), currency="USDT",
                   episode_intent_id=3, episode_started_at=now)
    # 昨天的退出意图属于第一批（id=1）；第二批的持有周期还没有退出意图，
    # 因此查询结果为 None —— 旧批次不再接管新一轮库存。
    scalars = iter([[inventory]])
    scalar = iter([new, None])

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalars(self, query):
            return next(scalars)

        async def scalar(self, query):
            return next(scalar)

    service = TradingService.__new__(TradingService)
    service.identity_ok = service.reconcile_ok = True
    service.environment = "paper"
    service.database = NS(sessionmaker=Session)
    service.strategies = {1: None}
    root = Path(__file__).resolve().parents[2]
    service.config = load_config(root / "config/config.example.yaml")
    service.quotes = NS(get_ticker=lambda *args: NS(bid_price=D("100")),
                        status=lambda *args: NS(usable=True))
    service.specs = {("spot", "BTCUSDT"): NS(
        qty_step=D("0.001"), tick_size=D("0.1"), delivery_time=None)}
    service.reservations = NS(_pending_close_qty=AsyncMock(return_value=D("0")))
    service._submit_draft = AsyncMock(return_value={"status": "ACCEPTED"})
    service._fault = AsyncMock()
    await service._exit_rules()
    call = service._submit_draft.call_args
    return {
        "scenario": "old_closed_purchase_triggers_exit_of_new_inventory",
        "new_inventory_age_seconds": 0,
        "configured_hold_seconds": 86400,
        "exit_submitted_to_mock": call is not None,
        "exit_quantity": str(call.args[2].qty) if call else None,
        "trigger_owner_intent": call.args[2].rationale.get("owner_intent_id") if call else None,
        "expected_after_fix": "exit_submitted_to_mock=false",
    }


def reproduce_option_fee():
    """官方非 VIP taker 期权费用公式：min(0.03% × 指数价, 7% × 权利金) × 数量。"""
    moment = now_utc()
    adapter = PaperAdapter(initial_balances={"USDT": D("1000")}, clock=lambda: moment)
    symbol = "BTC-TEST-100000-C"
    adapter.set_instrument(InstrumentSpec(
        "option", symbol, "BTC", "USDT", "Trading", settle_coin="USDT"))
    adapter.set_ticker(Ticker(
        "option", symbol, bid_price=D("100"), ask_price=D("101"),
        index_price=D("100000"), source_ts=moment, received_at=moment))
    order = adapter._register(OrderRequest(
        category="option", symbol=symbol, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, qty=D("1"), price=D("100"),
        order_link_id="local-fee-example"))
    adapter._apply_fill(order, D("100"), D("1"))
    simulated_fee = order.executions[0].exec_fee
    # Bybit official non-VIP taker formula, checked 2026-11-01:
    # min(0.03% * index price, 7% * option price) * traded size.
    official_example_fee = min(D("0.0003") * D("100000"), D("0.07") * D("100"))
    return {
        "scenario": "option_fee_example_non_vip_taker",
        "index_price": "100000", "option_price": "100", "quantity": "1",
        "paper_fee_usdt": str(simulated_fee),
        "official_formula_fee_usdt": str(official_example_fee),
        "fee_multiple": str(official_example_fee / simulated_fee),
        "expected_after_fix": "paper_fee_usdt == official_formula_fee_usdt",
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(reproduce_old_exit_rule()), ensure_ascii=False))
    print(json.dumps(reproduce_option_fee(), ensure_ascii=False))
