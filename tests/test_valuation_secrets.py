"""估值口径与凭据装载的单元测试。

覆盖权益、资金流调整损益与回撤公式（AC-17），以及受限凭据文件的
权限、属主、读取失败和脱敏行为（AC-21）。服务层基线与熔断另有集成测试。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import replace
from decimal import Decimal

import pytest

from bybitbot.adapters.base import AccountView, CoinBalance, PositionView
from bybitbot.settings.secrets import (
    ApiCredentials,
    CredentialError,
    load_credentials,
    redact,
)
from bybitbot.valuation import (
    ValuationInput,
    ValuationPolicy,
    adjusted_equity,
    day_key,
    day_pnl,
    day_start,
    drawdown_amount,
    value_account,
)

POLICY = ValuationPolicy(version="equity-v1", currency="USDT")


def account(*coins: CoinBalance, mode: str = "unified") -> AccountView:
    return AccountView(
        account_uid="uid-1",
        account_mode=mode,
        coins={coin.coin: coin for coin in coins},
    )


def cash(coin: str, amount: str, **kwargs) -> CoinBalance:
    return CoinBalance(
        coin=coin,
        equity=Decimal(amount),
        available=Decimal(amount),
        wallet_balance=Decimal(amount),
        **kwargs,
    )


# --------------------------------------------------------------------------
# value_account
# --------------------------------------------------------------------------


def test_equity_is_own_cash_plus_option_value_without_double_counting():
    result = value_account(
        POLICY,
        ValuationInput(
            account=replace(
                account(cash("USDT", "1000")),
                total_equity=Decimal("2000"),
                exchange_equity=Decimal("2000"),
            ),
            cash_rates={"USDT": Decimal("1")},
            option_positions=[
                PositionView(
                    category="option",
                    symbol="BTC-26NOV25-100000-C",
                    size=Decimal("2"),
                    mark_price=Decimal("500"),
                    settle_coin="USDT",
                )
            ],
            settle_rates={"USDT": Decimal("1")},
        ),
    )
    assert result.complete
    assert result.equity == Decimal("2000")
    assert result.currency == "USDT"
    assert result.policy_version == "equity-v1"
    # 交易所自报权益不参与计算，期权价值只加一次。
    assert result.details["options"]["BTC-26NOV25-100000-C"] == "1000"


def test_equity_is_incomplete_when_account_has_debt():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100", borrowed=Decimal("5"))),
            cash_rates={"USDT": Decimal("1")},
        ),
    )
    assert not result.complete
    assert result.equity == Decimal("0")
    assert "负债" in result.incomplete_reason


def test_missing_exchange_rate_marks_valuation_incomplete_not_zero():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100"), cash("BTC", "1")),
            cash_rates={"USDT": Decimal("1")},
        ),
    )
    assert not result.complete
    assert "BTC" in result.incomplete_reason
    # 已知部分仍计入权益，但不能被当作完整估值使用。
    assert result.equity == Decimal("100")


def test_zero_balance_without_rate_is_not_treated_as_missing():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100"), cash("BTC", "0")),
            cash_rates={"USDT": Decimal("1")},
        ),
    )
    assert result.complete
    assert result.equity == Decimal("100")


def test_missing_option_price_marks_valuation_incomplete():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100")),
            cash_rates={"USDT": Decimal("1")},
            option_positions=[PositionView(category="option", symbol="ETH-OPT", size=Decimal("1"))],
        ),
    )
    assert not result.complete
    assert "ETH-OPT:price" in result.incomplete_reason


@pytest.mark.parametrize("price", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_invalid_option_price_marks_valuation_incomplete(price):
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100")),
            cash_rates={"USDT": Decimal("1")},
            option_positions=[
                PositionView(
                    category="option", symbol="ETH-OPT", size=Decimal("1"), mark_price=price
                )
            ],
            settle_rates={"USDT": Decimal("1")},
        ),
    )
    assert not result.complete
    assert result.equity == Decimal("100")
    assert "ETH-OPT:无效价格、乘数或汇率" in result.incomplete_reason


def test_missing_settle_rate_marks_valuation_incomplete():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100")),
            cash_rates={"USDT": Decimal("1")},
            option_positions=[
                PositionView(
                    category="option",
                    symbol="BTC-OPT",
                    size=Decimal("1"),
                    mark_price=Decimal("10"),
                    settle_coin="USDC",
                )
            ],
            settle_rates={},
        ),
    )
    assert not result.complete
    assert "USDC" in result.incomplete_reason


def test_contract_multiplier_and_settle_rate_scale_option_value():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "0")),
            cash_rates={"USDT": Decimal("1")},
            option_positions=[
                PositionView(
                    category="option",
                    symbol="BTC-OPT",
                    size=Decimal("2"),
                    mark_price=Decimal("50"),
                    settle_coin="USDC",
                )
            ],
            settle_rates={"USDC": Decimal("0.9")},
            contract_multiplier={"BTC-OPT": Decimal("0.1")},
        ),
    )
    assert result.complete
    # 50 * 2 * 0.1 * 0.9 = 9
    assert result.equity == Decimal("9")


@pytest.mark.parametrize("size", [Decimal("-1"), Decimal("1")])
def test_short_option_position_makes_valuation_incomplete(size):
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100")),
            cash_rates={"USDT": Decimal("1")},
            option_positions=[
                PositionView(
                    category="option",
                    symbol="BTC-OPT",
                    size=size,
                    side="Sell" if size > 0 else None,
                    mark_price=Decimal("50"),
                    settle_coin="USDT",
                )
            ],
            settle_rates={"USDT": Decimal("1")},
        ),
    )
    assert not result.complete
    assert "不支持期权空头" in result.incomplete_reason


def test_accrued_interest_without_principal_makes_valuation_incomplete():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(cash("USDT", "100", accrued_interest=Decimal("2"))),
            cash_rates={"USDT": Decimal("1")},
        ),
    )
    assert not result.complete
    assert "负债" in result.incomplete_reason


def test_wallet_balance_missing_is_reported():
    result = value_account(
        POLICY,
        ValuationInput(
            account=account(
                CoinBalance(
                    coin="USDT",
                    equity=Decimal("100"),
                    available=Decimal("100"),
                    wallet_balance=None,
                )
            ),
            cash_rates={"USDT": Decimal("1")},
        ),
    )
    assert not result.complete
    assert "USDT:wallet_balance" in result.incomplete_reason


# --------------------------------------------------------------------------
# 日损益 / 回撤口径
# --------------------------------------------------------------------------


def test_day_pnl_subtracts_external_flow():
    # 权益从 1000 涨到 1100，但期间入金 50，真实损益是 50 而不是 100。
    assert day_pnl(
        equity_now=Decimal("1100"),
        equity_day_start=Decimal("1000"),
        net_external_flow=Decimal("50"),
    ) == Decimal("50")


def test_day_pnl_handles_outflow():
    # 提现 50 后权益从 1000 降到 900：其中 50 是提现、50 是真实亏损。
    assert day_pnl(
        equity_now=Decimal("900"),
        equity_day_start=Decimal("1000"),
        net_external_flow=Decimal("-50"),
    ) == Decimal("-50")


def test_adjusted_equity_removes_cumulative_flow():
    assert adjusted_equity(
        equity=Decimal("1100"), cumulative_net_external_flow=Decimal("50")
    ) == Decimal("1050")


def test_drawdown_is_peak_minus_current_and_never_negative():
    assert drawdown_amount(current_adjusted_equity=Decimal("80"), peak=Decimal("100")) == (
        Decimal("20")
    )
    assert drawdown_amount(current_adjusted_equity=Decimal("120"), peak=Decimal("100")) == (
        Decimal("0")
    )


def test_day_key_and_day_start_follow_configured_timezone():
    moment = dt.datetime(2026, 10, 31, 20, 0, tzinfo=dt.UTC)  # 北京时间 11-01 04:00
    assert day_key(moment, "Asia/Shanghai") == "2026-11-01"
    assert day_start(moment, "Asia/Shanghai") == dt.datetime(2026, 10, 31, 16, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------
# 凭据装载（SEC-01）
# --------------------------------------------------------------------------


def write_credentials(path):
    path.write_text(
        json.dumps({"api_key": "test-key", "api_secret": "test-api-secret"}), encoding="utf-8"
    )
    path.chmod(0o600)
    return path


def test_credentials_load_from_restricted_file(tmp_path):
    path = write_credentials(tmp_path / "credentials.json")
    creds = load_credentials(path)
    assert creds.api_key == "test-key"
    assert creds.api_secret == "test-api-secret"


def test_credentials_reject_broad_permissions(tmp_path):
    path = write_credentials(tmp_path / "credentials.json")
    path.chmod(0o644)
    with pytest.raises(CredentialError) as excinfo:
        load_credentials(path)
    assert "权限过宽" in str(excinfo.value)


def test_credentials_reject_group_or_world_readable_file(tmp_path):
    path = write_credentials(tmp_path / "credentials.json")
    path.chmod(0o640)
    with pytest.raises(CredentialError):
        load_credentials(path)


def test_credentials_reject_missing_file(tmp_path):
    with pytest.raises(CredentialError) as excinfo:
        load_credentials(tmp_path / "absent.json")
    assert "不存在" in str(excinfo.value)


def test_credentials_reject_missing_fields(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"api_key": "only-key"}), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(CredentialError) as excinfo:
        load_credentials(path)
    assert "缺少" in str(excinfo.value)


def test_credentials_reject_invalid_json(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(CredentialError):
        load_credentials(path)


def test_credentials_repr_and_summary_never_show_secret():
    creds = ApiCredentials(api_key="abcdef123456", api_secret="super-secret-value")
    assert "super-secret-value" not in repr(creds)
    assert "super-secret-value" not in str(creds)
    summary = creds.masked_summary()
    assert summary["api_key_prefix"] == "abcdef***"
    assert "super-secret-value" not in json.dumps(summary)


def test_permission_check_can_be_disabled_explicitly(tmp_path):
    path = write_credentials(tmp_path / "credentials.json")
    path.chmod(0o644)
    creds = load_credentials(path, require_permissions=False)
    assert creds.api_key == "test-key"


def test_credential_file_owner_is_checked(tmp_path, monkeypatch):
    path = write_credentials(tmp_path / "credentials.json")
    actual_uid = path.stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(CredentialError, match="不属于当前用户"):
        load_credentials(path)


def test_redact_removes_known_secrets_but_keeps_short_ones():
    text = "key=super-secret-value and short=abc"
    result = redact(text, ["super-secret-value", "abc"])
    assert "super-secret-value" not in result
    assert "***REDACTED***" in result
    assert "short=abc" in result
    assert redact("", ["x"]) == ""


def test_redact_ignores_empty_secret_list():
    assert redact("plain text", []) == "plain text"


def test_credential_read_failure_is_reported_without_loading_secret(tmp_path, monkeypatch):
    path = write_credentials(tmp_path / "credentials.json")

    def denied(*args, **kwargs):
        raise PermissionError("synthetic read denied")

    monkeypatch.setattr(type(path), "read_text", denied)
    with pytest.raises(CredentialError, match="无法读取"):
        load_credentials(path)
