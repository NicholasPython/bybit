"""期权筛选与风控上限的回归（费用占比、Delta 优先、权益比例、止损规则）。

这些用例对应真实账户复盘得到的结论：最便宜的合约是临近到期的彩票，
双边费用吃掉权利金，没有止损也没有敞口上限。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from bybitbot.adapters.base import AccountView, CoinBalance, InstrumentSpec, Ticker
from bybitbot.db.models import TradeIntent
from bybitbot.fees import FeeModel
from bybitbot.market import QuoteCache
from bybitbot.risk.engine import RiskContext, RiskEngine
from bybitbot.service import DecisionView
from bybitbot.settings.loader import load_config
from bybitbot.settings.models import ExitRuleConfig, StrategyConfig
from bybitbot.strategy.base import SignalContext
from bybitbot.strategy.templates import OptionBuyParams, ScheduledOptionBuyStrategy

MOMENT = dt.datetime(2026, 11, 2, 8, tzinfo=dt.UTC)
#: ETH 指数价；期权费用按指数价 × 0.03% 计收。
ETH_INDEX = D("2300")


def fees() -> FeeModel:
    return FeeModel(version="test-v1", spot_taker_rate=D("0.001"))


def option_instrument(strike: str, dte: int = 25) -> InstrumentSpec:
    return InstrumentSpec(
        "option",
        f"ETH-27NOV26-{strike}-C-USDT",
        "ETH",
        "USDT",
        "Trading",
        settle_coin="USDT",
        tick_size=D("0.1"),
        qty_step=D("0.1"),
        min_order_qty=D("0.1"),
        options_type="Call",
        strike_price=D(strike),
        delivery_time=MOMENT + dt.timedelta(days=dte),
    )


def option_ticker(bid: D, ask: D, *, delta: D = D("0.45"), index: D | None = ETH_INDEX) -> Ticker:
    return Ticker(
        "option",
        "ETH-27NOV26-2400-C-USDT",
        bid_price=bid,
        ask_price=ask,
        bid_size=D("100"),
        ask_size=D("100"),
        mark_price=ask,
        delta=delta,
        iv=D("0.6"),
        index_price=index,
        source_ts=MOMENT,
        received_at=MOMENT,
    )


def strategy(params: OptionBuyParams, specs, tickers) -> list:
    quotes = QuoteCache()
    for ticker in tickers:
        quotes.put_ticker(ticker)
    context = SignalContext("paper", "synthetic", 1, 1, "controls", MOMENT)
    return strategy_object(params, specs, quotes, context)


def strategy_object(params, specs, quotes, context):
    return ScheduledOptionBuyStrategy("controls", params).evaluate(
        context, DecisionView(specs, quotes, MOMENT)
    )


def specs_for(*instruments):
    return {("option", item.symbol): item for item in instruments}


def test_fee_ratio_cap_rejects_contract_whose_fee_eats_premium():
    """权利金 20 USDT、指数价 2300 时，每边费用占权利金 3.45%，超过 1% 上限。"""
    instrument = option_instrument("2400")
    params = OptionBuyParams(
        underlying="ETH",
        enabled=True,
        premium_budget=D("20"),
        fee_model=fees(),
        max_fee_ratio=D("0.01"),
    )
    drafts = strategy(
        params,
        specs_for(instrument),
        [option_ticker(D("19.9"), D("20"))],
    )
    assert drafts == []


def test_fee_ratio_cap_accepts_contract_when_premium_large_enough():
    """权利金 100 USDT 时费用占 0.69%，通过上限。"""
    instrument = option_instrument("2400")
    params = OptionBuyParams(
        underlying="ETH",
        enabled=True,
        premium_budget=D("200"),
        fee_model=fees(),
        max_fee_ratio=D("0.01"),
    )
    drafts = strategy(
        params,
        specs_for(instrument),
        [option_ticker(D("99.9"), D("100"))],
    )
    assert len(drafts) == 1
    assert drafts[0].rationale["max_fee_ratio"] == "0.01"


def test_missing_index_price_is_treated_as_over_the_cap():
    """缺标的指数价时按 7% 封顶估算，必然超限 —— 保守方向。"""
    instrument = option_instrument("2400")
    params = OptionBuyParams(
        underlying="ETH",
        enabled=True,
        premium_budget=D("200"),
        fee_model=fees(),
        max_fee_ratio=D("0.01"),
    )
    drafts = strategy(
        params,
        specs_for(instrument),
        [option_ticker(D("99.9"), D("100"), index=None)],
    )
    assert drafts == []


def _two_candidates():
    cheap = option_instrument("2400")
    rich = option_instrument("2600")
    quotes = QuoteCache()
    quotes.put_ticker(
        Ticker(
            "option",
            cheap.symbol,
            bid_price=D("9.9"),
            ask_price=D("10"),
            bid_size=D("100"),
            ask_size=D("100"),
            delta=D("0.20"),
            iv=D("0.6"),
            index_price=ETH_INDEX,
            source_ts=MOMENT,
            received_at=MOMENT,
        )
    )
    quotes.put_ticker(
        Ticker(
            "option",
            rich.symbol,
            bid_price=D("89.9"),
            ask_price=D("90"),
            bid_size=D("100"),
            ask_size=D("100"),
            delta=D("0.45"),
            iv=D("0.6"),
            index_price=ETH_INDEX,
            source_ts=MOMENT,
            received_at=MOMENT,
        )
    )
    return specs_for(cheap, rich), quotes


def test_target_delta_wins_over_cheapest_premium():
    """两个合约都在 Delta 区间内时，选最接近目标 Delta 的那个，不是最便宜的。"""
    specs, quotes = _two_candidates()
    params = OptionBuyParams(
        underlying="ETH",
        enabled=True,
        premium_budget=D("200"),
        fee_model=fees(),
        target_delta=D("0.45"),
        delta_tolerance=D("0.25"),
    )
    (draft,) = strategy_object(params, specs, quotes, _context())
    assert draft.symbol == "ETH-27NOV26-2600-C-USDT"
    assert "|delta-target|" in draft.rationale["sort_rule"]


def test_cheapest_premium_wins_when_delta_priority_is_off():
    specs, quotes = _two_candidates()
    params = OptionBuyParams(
        underlying="ETH",
        enabled=True,
        premium_budget=D("200"),
        fee_model=fees(),
        target_delta=D("0.45"),
        delta_tolerance=D("0.25"),
        prefer_target_delta=False,
    )
    (draft,) = strategy_object(params, specs, quotes, _context())
    assert draft.symbol == "ETH-27NOV26-2400-C-USDT"
    assert draft.rationale["sort_rule"] == "ask asc, expiry asc, symbol asc"


def _context():
    return SignalContext("paper", "synthetic", 1, 1, "controls", MOMENT)


# --------------------------------------------------------------------------
# 配置校验
# --------------------------------------------------------------------------


def _strategy(**params) -> StrategyConfig:
    values = {
        "name": "eth-call-weekly",
        "kind": "option_single_leg_buy",
        "symbols": ("ETH",),
        "schedule_utc": ("00:00",),
        "signal_ttl_seconds": 120,
        "cooldown_seconds": 604800,
        "budget_per_run": {"value": "20", "currency": "USDT"},
        "max_spend_per_trade": {"value": "20", "currency": "USDT"},
        "exit_rules": ({"kind": "before_expiry"},),
    }
    values.update(params)
    return StrategyConfig(**values)


@pytest.mark.parametrize(
    "params",
    [
        {"max_fee_ratio": "0.07"},
        {"max_fee_ratio": "0.5"},
        {"delta_low": "0.7", "delta_high": "0.3"},
        {"strike_low_pct": "120", "strike_high_pct": "90"},
        {"min_days_to_expiry": "30", "max_days_to_expiry": "10"},
        {"min_days_to_expiry": "1.5"},
        {"unknown_key": "1"},
    ],
)
def test_invalid_option_params_are_rejected(params):
    with pytest.raises(ValueError):
        _strategy(params=params)


def test_valid_option_params_are_accepted():
    cfg = _strategy(
        params={
            "options_type": "Call",
            "min_days_to_expiry": "7",
            "max_days_to_expiry": "45",
            "delta_low": "0.30",
            "delta_high": "0.60",
            "strike_low_pct": "90",
            "strike_high_pct": "110",
            "min_premium": "50",
            "max_premium": "200",
            "max_fee_ratio": "0.01",
        }
    )
    assert cfg.params["max_fee_ratio"] == "0.01"


def test_premium_stop_loss_requires_threshold():
    with pytest.raises(ValueError):
        ExitRuleConfig(kind="premium_stop_loss")
    rule = ExitRuleConfig(kind="premium_stop_loss", stop_loss_pct=D("25"))
    assert rule.stop_loss_pct == D("25")
    with pytest.raises(ValueError):
        ExitRuleConfig(kind="premium_stop_loss", stop_loss_pct=D("0"))
    with pytest.raises(ValueError):
        ExitRuleConfig(kind="premium_stop_loss", stop_loss_pct=D("100"))


def test_premium_stop_loss_is_rejected_for_spot_strategy():
    """权利金止损按期权买一价判定，现货策略写了也是永不触发 —— 配置期就要拦下。"""
    with pytest.raises(ValueError):
        StrategyConfig(
            name="eth-spot-dca",
            kind="spot_fixed_budget_dca",
            symbols=("ETHUSDT",),
            schedule_utc=("00:00",),
            signal_ttl_seconds=120,
            cooldown_seconds=0,
            budget_per_run={"value": "20", "currency": "USDT"},
            max_spend_per_trade={"value": "20", "currency": "USDT"},
            exit_rules=({"kind": "premium_stop_loss", "stop_loss_pct": "25"},),
        )


def test_premium_stop_loss_is_accepted_for_option_strategy():
    cfg = _strategy(
        exit_rules=(
            {"kind": "premium_stop_loss", "stop_loss_pct": "25"},
            {"kind": "before_expiry"},
        )
    )
    assert cfg.exit_rules[0].kind == "premium_stop_loss"


# --------------------------------------------------------------------------
# 权益比例限额
# --------------------------------------------------------------------------


def test_option_fee_ratio_cap_must_be_below_premium_cap():
    config = load_config("config/config.example.yaml")
    assert config.option.max_fee_ratio == D("0.01")
    assert config.risk.max_premium_to_equity_ratio == D("0.2")


# --------------------------------------------------------------------------
# 权益比例限额（RSK）
# --------------------------------------------------------------------------


def _option_intent(**kwargs):
    values = {
        "environment": "paper",
        "account_uid": "paper-account",
        "signal_id": "equity-ratio-test",
        "action": "OPTION_BUY_OPEN",
        "category": "option",
        "symbol": "ETH-27NOV26-2400-C-USDT",
        "side": "Buy",
        "qty": D("0.1"),
        "qty_unit": "contract",
        "price_limit": D("300"),
        "spend_limit": D("30"),
        "currency": "USDT",
        "underlying": "ETH",
        "delivery_time": dt.datetime.now(dt.UTC) + dt.timedelta(days=20),
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1),
        "status": "APPROVED",
    }
    values.update(kwargs)
    return TradeIntent(**values)


def _risk_context(**kwargs):
    moment = dt.datetime.now(dt.UTC)
    values = {
        "account": AccountView(
            "paper-account",
            "unified",
            {"USDT": CoinBalance("USDT", D("1000"), D("1000"), D("1000"))},
        ),
        "positions": [],
        "quotes": {
            "ETH-27NOV26-2400-C-USDT": Ticker(
                "option",
                "ETH-27NOV26-2400-C-USDT",
                bid_price=D("299"),
                ask_price=D("300"),
                bid_size=D("100"),
                ask_size=D("100"),
                delta=D("0.45"),
                iv=D("0.6"),
                index_price=ETH_INDEX,
                source_ts=moment,
                received_at=moment,
            )
        },
        "open_orders": [],
        "valuation_trusted": True,
        "identity_trusted": True,
        "account_delta_per_underlying": {"ETH": D("0")},
        "risk_equity": D("100"),
    }
    values.update(kwargs)
    return RiskContext(**values)


async def test_premium_above_equity_ratio_is_rejected():
    engine = RiskEngine(load_config("config/config.example.yaml"))
    result = await engine.evaluate(_option_intent(), _risk_context())
    checks = {item.code: item for item in result.checks}
    assert checks["PREMIUM_EQUITY_RATIO"].approved is False
    assert "权益" in checks["PREMIUM_EQUITY_RATIO"].detail


async def test_premium_within_equity_ratio_passes_the_check():
    engine = RiskEngine(load_config("config/config.example.yaml"))
    result = await engine.evaluate(_option_intent(), _risk_context(risk_equity=D("1000")))
    checks = {item.code: item for item in result.checks}
    assert checks["PREMIUM_EQUITY_RATIO"].approved is True


async def test_missing_risk_equity_blocks_option_open():
    engine = RiskEngine(load_config("config/config.example.yaml"))
    result = await engine.evaluate(_option_intent(), _risk_context(risk_equity=D("0")))
    checks = {item.code: item for item in result.checks}
    assert checks["PREMIUM_EQUITY_RATIO"].approved is False
    assert "权益不可用" in checks["PREMIUM_EQUITY_RATIO"].detail
