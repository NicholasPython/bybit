"""Private console: absolute sessions, write protection, queue semantics and safe evidence."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from bybitbot.db.models import (
    AccountSnapshot,
    ControlCommand,
    Execution,
    Instrument,
    Order,
    QuoteSnapshot,
    ReconciliationRun,
    RiskDecision,
    RiskPause,
    RuntimeState,
    Strategy,
    StrategyConfigVersion,
    TradeIntent,
)
from bybitbot.settings.loader import load_config
from bybitbot.web import app as web_app
from bybitbot.web.app import SESSION_COOKIE, SESSION_SECONDS, create_app

ORIGIN = "https://console.example.test"
PASSWORD = "synthetic-test-admin-password"


@pytest.fixture
def config():
    return load_config(Path(__file__).resolve().parents[1] / "config/config.example.yaml")


@pytest.fixture
def moment():
    return [dt.datetime(2026, 10, 31, 8, 0, tzinfo=dt.UTC)]


@pytest.fixture
def auth_path(tmp_path):
    salt = b"synthetic-auth-salt-for-tests"
    digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 600_000)
    path = tmp_path / "admin.json"
    path.write_text(
        json.dumps(
            {
                "username": "operator",
                "password_hash": f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}",
                "session_secret": "synthetic-session-secret-with-more-than-thirty-two-characters",
            }
        )
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def app(config, database, auth_path, moment):
    return create_app(config, database, auth_path, clock=lambda: moment[0])


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as browser:
        yield browser


async def login(client):
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def command(kind="PAUSE_NEW_RISK", payload=None, **changes):
    return {
        "command_id": str(uuid4()),
        "kind": kind,
        "payload": payload if payload is not None else {"reason": "人工核查"},
        "confirmation": None if kind == "PAUSE_NEW_RISK" else "确认",
        **changes,
    }


async def test_console_requires_session_and_serves_local_assets(client):
    assert (await client.get("/")).status_code == 303
    for path in ("/api/status", "/api/session", "/api/audit.csv"):
        assert (await client.get(path)).status_code == 401
    response = await client.get("/login")
    assert response.status_code == 200
    assert "管理员登录" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    for path, marker in (
        ("/static/console.css", "--navy: #15345c"),
        ("/static/console.js", "crypto.randomUUID"),
    ):
        response = await client.get(path)
        assert response.status_code == 200
        assert marker in response.text
    assert (await client.get("/openapi.json")).status_code == 404
    await login(client)
    response = await client.get("/")
    assert response.status_code == 200
    assert "首次风险基线" in response.text
    assert "最近行情时效" in response.text


async def test_login_cookie_and_error_responses_hide_password(client):
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": "private-wrong-value"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401
    assert "private-wrong-value" not in response.text
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": PASSWORD, "extra": "private"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 422
    assert PASSWORD not in response.text
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    cookie = response.headers["set-cookie"]
    assert response.status_code == 200
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=1800" in cookie
    assert PASSWORD not in response.text
    assert "password_hash" not in response.text


async def test_sessions_expire_absolutely_and_logout_revokes(client, moment):
    headers = await login(client)
    cookie = client.cookies.get(SESSION_COOKIE)
    moment[0] += dt.timedelta(seconds=SESSION_SECONDS - 1)
    assert (await client.get("/api/session")).status_code == 200
    moment[0] += dt.timedelta(seconds=1)
    assert (await client.get("/api/session")).status_code == 401
    headers = await login(client)
    assert client.cookies.get(SESSION_COOKIE) != cookie
    cookie = client.cookies.get(SESSION_COOKIE)
    assert (await client.post("/api/logout", headers=headers)).status_code == 204
    client.cookies.set(SESSION_COOKIE, cookie)
    assert (await client.get("/api/session")).status_code == 401


async def test_web_restart_invalidates_old_session(config, database, auth_path, moment, client):
    await login(client)
    cookie = client.cookies.get(SESSION_COOKIE)
    restarted = create_app(config, database, auth_path, clock=lambda: moment[0])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted),
        base_url=ORIGIN,
        cookies={SESSION_COOKIE: cookie},
    ) as browser:
        assert (await browser.get("/api/session")).status_code == 401


@pytest.mark.parametrize("origin", [None, "https://other.example.test", "null", ORIGIN + "/path"])
async def test_login_requires_exact_origin(client, origin):
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": PASSWORD},
        headers={"Origin": origin} if origin is not None else {},
    )
    assert response.status_code == 403


async def test_login_rate_limit_is_bounded_and_recovers(client, moment):
    for _ in range(8):
        response = await client.post(
            "/api/login",
            json={"username": "operator", "password": "wrong"},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 401
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"
    moment[0] += dt.timedelta(seconds=301)
    await login(client)


@pytest.mark.parametrize(
    "overrides",
    [
        {"Origin": ORIGIN},
        {"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        {"Origin": "https://other.example.test"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
async def test_control_requires_origin_and_csrf(client, database, overrides):
    headers = await login(client)
    if overrides == {"Origin": ORIGIN}:
        headers = overrides
    else:
        headers.update(overrides)
    response = await client.post("/api/commands", json=command(), headers=headers)
    assert response.status_code == 403
    async with database.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(ControlCommand)) == 0
    assert (await client.post("/api/logout", headers={"Origin": ORIGIN})).status_code == 403


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("PAUSE_NEW_RISK", {"reason": "人工暂停"}),
        ("CANCEL_BOT_OPEN_ORDERS", {"strategy_id": 4, "symbol": "BTCUSDT"}),
        ("CANCEL_ORDER", {"order_link_ids": ["paper-physical-123"]}),
        (
            "REDUCE",
            {
                "strategy_id": 4,
                "category": "option",
                "symbol": "BTC-31OCT26-80000-C",
                "qty": "0.01000001",
                "price_floor": "125.5",
                "reason": "期限退出",
            },
        ),
        ("RESUME", {"pause_id": 3, "reason": "对账完成后核验"}),
        ("CONFIRM_TAKEOVER", {"evidence": "已检查旧 worker PID 结束", "reason": "服务接管"}),
        ("INITIALIZE_BASELINE", {"reason": "明确从当前时点建立首次风险起点"}),
        (
            "REGISTER_EXTERNAL_CASH_FLOW",
            {
                "asset": "USDT",
                "amount": "5.55",
                "kind": "TRANSFER_IN",
                "reference": "bybit-transfer-20261101",
                "reason": "主账户划入子账户",
            },
        ),
    ],
)
async def test_all_commands_only_enqueue_once(client, database, kind, payload):
    headers = await login(client)
    body = command(kind, payload)
    responses = await asyncio.gather(
        *(client.post("/api/commands", json=body, headers=headers) for _ in range(5))
    )
    assert {response.status_code for response in responses} == {202}
    assert sum(response.json()["created"] for response in responses) == 1
    for response in responses:
        view = response.json()["command"]
        assert view["status"] == "PENDING"
        assert view["requested_by"] == "operator"
        assert view["command_id"] == body["command_id"]
        assert view["payload"] == payload
    async with database.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(ControlCommand)) == 1
        assert await session.scalar(select(func.count()).select_from(TradeIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(RiskPause)) == 0
    changed = {**body, "kind": "PAUSE_NEW_RISK", "payload": {"reason": "不同范围"}}
    response = await client.post("/api/commands", json=changed, headers=headers)
    assert response.status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmation": None},
        {"confirmation": "确定"},
        {"command_id": "not-a-uuid"},
        {"kind": "DIRECT_CREATE_ORDER"},
        {"payload": {"order_link_ids": ["one", "one"]}},
        {"payload": {"order_link_ids": [], "isLeverage": 1}},
    ],
)
async def test_commands_reject_invalid_scope_and_confirmation(client, changes):
    headers = await login(client)
    body = {**command("CANCEL_ORDER", {"order_link_ids": ["one"]}), **changes}
    assert (await client.post("/api/commands", json=body, headers=headers)).status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"asset": "USDT", "amount": "5.55", "kind": "TRANSFER_IN"},  # 缺少流水标识与原因
        {"asset": "USDT", "amount": "-5.55", "kind": "TRANSFER_IN",
         "reference": "r", "reason": "x"},
        {"asset": "USDT", "amount": "0", "kind": "TRANSFER_IN", "reference": "r", "reason": "x"},
        {"asset": "USDT", "amount": "5.55", "kind": "UNKNOWN", "reference": "r", "reason": "x"},
        {"asset": "usdt!", "amount": "5.55", "kind": "TRANSFER_IN",
         "reference": "r", "reason": "x"},
        {"asset": "USDT", "amount": "5.55", "kind": "TRANSFER_IN", "reference": "", "reason": "x"},
        {"asset": "USDT", "amount": "5.55", "kind": "TRANSFER_IN", "reference": "r", "reason": "x",
         "extra": "not-allowed"},
    ],
)
async def test_external_cash_flow_payload_is_validated(client, payload):
    """外部资金流登记只接受估值币种、正金额和已知类型。"""
    headers = await login(client)
    body = command("REGISTER_EXTERNAL_CASH_FLOW", payload)
    assert (await client.post("/api/commands", json=body, headers=headers)).status_code == 422


async def test_external_cash_flow_requires_explicit_confirmation(client):
    headers = await login(client)
    body = command(
        "REGISTER_EXTERNAL_CASH_FLOW",
        {
            "asset": "USDT",
            "amount": "5.55",
            "kind": "TRANSFER_IN",
            "reference": "bybit-transfer-20261101",
            "reason": "主账户划入",
        },
    )
    body["confirmation"] = None
    assert (await client.post("/api/commands", json=body, headers=headers)).status_code == 422
    body["confirmation"] = "确认"
    assert (await client.post("/api/commands", json=body, headers=headers)).status_code == 202


@pytest.mark.parametrize("qty", ["0", "-1", "NaN", "Infinity", "1e9", "0.0000000000000000001", 1.2])
async def test_reduce_requires_positive_finite_exact_decimal_strings(client, qty):
    headers = await login(client)
    body = command(
        "REDUCE",
        {
            "strategy_id": 1,
            "category": "spot",
            "symbol": "BTCUSDT",
            "qty": qty,
            "price_floor": "50000",
            "reason": "减仓",
        },
    )
    assert (await client.post("/api/commands", json=body, headers=headers)).status_code == 422


async def test_request_body_limit_and_json_type(client):
    headers = await login(client)
    response = await client.post(
        "/api/commands",
        content="x" * 20_000,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    response = await client.post("/api/commands", content="{}", headers=headers)
    assert response.status_code == 415


async def test_public_health_only_reports_liveness(client, database, moment):
    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}
    async with database.sessionmaker() as session:
        row = RuntimeState(
            environment="paper",
            key="status",
            value={"account_uid": "private"},
            updated_at=moment[0],
        )
        session.add(row)
        await session.commit()
        row_id = row.id
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    moment[0] += dt.timedelta(seconds=120)
    assert (await client.get("/health")).status_code == 503
    async with database.sessionmaker() as session:
        row = await session.get(RuntimeState, row_id)
        row.updated_at = moment[0] + dt.timedelta(seconds=10)
        await session.commit()
    assert (await client.get("/health")).status_code == 503


async def test_status_is_environment_scoped_and_allowlisted(client, database, moment):
    async with database.sessionmaker() as session:
        session.add_all(
            [
                RuntimeState(
                    environment="paper",
                    key="status",
                    updated_at=moment[0],
                    value={
                        "state": "PAUSED_OPEN",
                        "account_uid": "paper-account",
                        "lease": "NEEDS_CONFIRMATION",
                        "source": "paper 模拟",
                        "equity": "1000",
                        "daily_pnl": None,
                        "drawdown": None,
                        "valuation_complete": True,
                        "baseline_available": False,
                        "last_error": "api_secret=sensitive-runtime",
                        "session_secret": "sensitive-runtime",
                    },
                ),
                RuntimeState(
                    environment="live", key="status", value={"account_uid": "other-live-uid"}
                ),
                AccountSnapshot(
                    environment="paper",
                    account_uid="paper-account",
                    account_mode="unified",
                    total_equity=Decimal("1000"),
                    complete=True,
                    captured_at=moment[0],
                    coins={
                        "USDT": {
                            "equity": "1000",
                            "available": "999",
                            "locked": "1",
                            "borrowed": "0",
                            "private_field": "sensitive-wallet",
                        }
                    },
                    raw={"api_key": "sensitive-wallet"},
                ),
                RiskPause(
                    environment="paper",
                    scope="ACCOUNT",
                    reason_code="UNKNOWN_ORDER",
                    reason_detail="api_key=sensitive-pause",
                    locked=True,
                    evidence={"secret": "sensitive-pause"},
                ),
                RiskPause(
                    environment="live",
                    scope="ACCOUNT",
                    reason_code="EXTERNAL",
                    reason_detail="other-live-pause",
                    locked=True,
                ),
                ReconciliationRun(
                    environment="paper",
                    scope="full",
                    allows_resume=False,
                    differences=[
                        {
                            "kind": "MISSING_EXECUTION",
                            "detail": "需补齐交易事实",
                            "severity": "CRITICAL",
                            "raw": "sensitive-recon",
                        }
                    ],
                ),
                Instrument(
                    environment="paper",
                    category="spot",
                    symbol="BTCUSDT",
                    base_coin="BTC",
                    quote_coin="USDT",
                    status="Trading",
                    raw={"secret": "sensitive-spec"},
                ),
                QuoteSnapshot(
                    environment="paper",
                    category="spot",
                    symbol="BTCUSDT",
                    source_ts=moment[0] + dt.timedelta(seconds=10),
                    received_at=moment[0],
                    raw={"secret": "sensitive-quote"},
                ),
            ]
        )
        await session.commit()
    await login(client)
    response = await client.get("/api/status")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["environment"] == "paper"
    assert data["account"]["coins"][0]["available"] == "999"
    assert data["runtime"]["baseline_available"] is False
    assert data["runtime"]["daily_pnl"] is None
    assert data["baseline"] is None
    assert data["pauses"][0]["reason_detail"].startswith("敏感诊断内容已隐藏")
    assert data["reconciliations"][0]["differences"][0]["detail"] == "需补齐交易事实"
    assert data["quotes"][0]["stale"] is True
    assert data["instruments"][0]["base_coin"] == "BTC"
    for private in ("sensitive-", "other-live-", "db_dsn", "credential_ref", "password_hash"):
        assert private not in response.text


async def test_completed_command_preserves_actual_results(client, database):
    headers = await login(client)
    body = command("CANCEL_ORDER", {"order_link_ids": ["one", "two"]})
    await client.post("/api/commands", json=body, headers=headers)
    async with database.sessionmaker() as session:
        row = await session.scalar(select(ControlCommand))
        row.status = "EXECUTED"
        row.result = {
            "terminal_confirmed": ["one"],
            "pending": [{"order_link_id": "two", "detail": "仍待核实"}],
            "scope": "CANCEL_ORDER",
            "api_key": "sensitive-command",
        }
        await session.commit()
    response = await client.post("/api/commands", json=body, headers=headers)
    assert response.status_code == 200
    result = response.json()["command"]["result"]
    assert result["terminal_confirmed"] == ["one"]
    assert result["pending"][0]["order_link_id"] == "two"
    assert "sensitive-command" not in response.text


async def test_csv_trace_joins_and_escapes_formulas(client, database, moment):
    async with database.sessionmaker() as session:
        strategy = Strategy(environment="paper", name="synthetic", kind="spot_fixed_budget_dca")
        session.add(strategy)
        await session.flush()
        version = StrategyConfigVersion(
            strategy_id=strategy.id,
            version=1,
            params={},
            budget_per_run=10,
            max_spend_per_trade=10,
            currency="USDT",
        )
        session.add(version)
        intent = TradeIntent(
            environment="paper",
            account_uid="paper-account",
            strategy_id=strategy.id,
            strategy_version=1,
            signal_id='=HYPERLINK("synthetic")',
            action="SPOT_BUY",
            category="spot",
            symbol="BTCUSDT",
            side="Buy",
            qty=Decimal("0.0001"),
            qty_unit="base_coin",
            price_limit=50000,
            spend_limit=5,
            currency="USDT",
            expires_at=moment[0] + dt.timedelta(seconds=60),
            status="APPROVED",
        )
        session.add(intent)
        await session.flush()
        session.add(
            RiskDecision(
                intent_id=intent.id,
                decision="APPROVED",
                reason_code="OK",
                config_version="synthetic-v1",
            )
        )
        order = Order(
            environment="paper",
            intent_id=intent.id,
            order_link_id="one",
            category="spot",
            symbol="BTCUSDT",
            side="Buy",
            order_type="Limit",
            time_in_force="GTC",
            price=50000,
            qty=Decimal("0.0001"),
            qty_filled=Decimal("0.0001"),
            status="FILLED",
            sync_state="CONFIRMED",
        )
        session.add(order)
        await session.flush()
        session.add(
            Execution(
                environment="paper",
                category="spot",
                exchange_exec_id="exec-one",
                order_id=order.id,
                order_link_id="one",
                symbol="BTCUSDT",
                side="Buy",
                exec_price=50000,
                exec_qty=Decimal("0.0001"),
                exec_fee=Decimal("0.0000001"),
                exec_fee_asset="BTC",
                exec_time=moment[0],
                raw={"api_key": "csv-secret"},
            )
        )
        session.add(
            TradeIntent(
                environment="live",
                account_uid="live-private",
                signal_id="other-live",
                action="SPOT_BUY",
                category="spot",
                symbol="BTCUSDT",
                side="Buy",
                qty=1,
                qty_unit="base_coin",
                spend_limit=1,
                currency="USDT",
                expires_at=moment[0],
                status="REJECTED",
            )
        )
        await session.commit()
    await login(client)
    response = await client.get("/api/audit.csv")
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
    assert len(rows) == 1
    row = rows[0]
    assert row["signal_id"].startswith("'=HYPERLINK")
    assert row["strategy_version"] == "1"
    assert row["risk_config_version"] == "synthetic-v1"
    assert row["order_link_id"] == "one"
    assert row["exchange_exec_id"] == "exec-one"
    assert row["fee_asset"] == "BTC"
    assert Decimal(row["fee"]) == Decimal("0.0000001")
    assert "csv-secret" not in response.text and "other-live" not in response.text
    assert response.headers["x-export-truncated"] == "false"
    assert (await client.get("/api/audit.csv?limit=5001")).status_code == 422


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660])
def test_admin_file_rejects_broad_or_wrong_mode(config, database, auth_path, mode):
    auth_path.chmod(mode)
    with pytest.raises(ValueError, match="认证文件无效"):
        create_app(config, database, auth_path)


def test_admin_file_rejects_symlink_and_malformed_hash(config, database, auth_path, tmp_path):
    link = tmp_path / "link.json"
    link.symlink_to(auth_path)
    with pytest.raises(ValueError, match="认证文件无效"):
        create_app(config, database, link)
    auth_path.write_text(
        json.dumps(
            {"username": "operator", "password_hash": 5, "session_secret": "long-secret" * 5}
        )
    )
    with pytest.raises(ValueError, match="认证文件无效"):
        create_app(config, database, auth_path)


class _FakeValidationLifecycle:
    """验证层测试替身；记录 Web 传入的环境与账户隔离范围。"""

    calls = []

    def __init__(self, database):
        self.database = database

    @classmethod
    async def status(cls, database, *, environment, account_uid):
        cls.calls.append(("status", environment, account_uid))
        return {
            "studies": [
                {
                    "run_id": "paper-run-1",
                    "environment": environment,
                    "account_uid": account_uid,
                    "status": "running",
                    "policy": "weak_down",
                    "direction": "Put",
                    "bases": ["BTC", "ETH"],
                    "heartbeat_ms": 1_764_583_200_000,
                    "gap_count": 3,
                    "chain_head": "abcdef1234567890",
                    "positions": [{"base": "BTC", "phase": "waiting"}],
                    "frozen": True,
                    "final_report_ready": False,
                    "last_error": "sample_failed:api_key=private",
                    "protocol": {"description": "private-protocol-body"},
                },
                {
                    "run_id": "other-run",
                    "environment": "live",
                    "account_uid": "other-account",
                    "status": "running",
                },
            ]
        }

    @classmethod
    async def export(cls, database, run_id, *, environment, account_uid):
        cls.calls.append(("export", run_id, environment, account_uid))
        if run_id == "broken-ledger":
            raise ValueError("验证账本校验失败")
        if run_id == "not-found":
            raise ValueError("验证运行不存在")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("STATUS.md", "synthetic validation archive")
        return output.getvalue()


@pytest.fixture
def fake_lifecycle(monkeypatch):
    _FakeValidationLifecycle.calls = []
    monkeypatch.setattr(web_app, "get_validation_lifecycle", lambda: _FakeValidationLifecycle)
    return _FakeValidationLifecycle


async def test_console_serves_validation_section(client):
    await login(client)
    response = await client.get("/")
    assert response.status_code == 200
    for marker in (
        'id="tab-validation"',
        'id="validation-body"',
        'data-command="VALIDATION_START"',
        'data-command="VALIDATION_STOP"',
        "id=\"validation-error\"",
    ):
        assert marker in response.text


async def test_validation_status_is_scoped_and_sanitized(client, fake_lifecycle):
    await login(client)
    response = await client.get("/api/status")
    assert response.status_code == 200, response.text
    validation = response.json()["validation"]
    assert [row["run_id"] for row in validation["studies"]] == ["paper-run-1"]
    row = validation["studies"][0]
    assert row["status"] == "running"
    assert row["phases"] == [{"base": "BTC", "phase": "waiting"}]
    assert row["heartbeat_at"] == "2025-12-01T10:00:00.000Z"
    assert row["gap_count"] == 3
    assert row["chain_head"] == "abcdef123456"
    assert row["last_error"] == "验证服务报告异常；请查看服务器日志。"
    for private in ("private-protocol-body", "api_key", "private", "api-key", "other-run"):
        assert private not in response.text
    assert fake_lifecycle.calls == [("status", "paper", "paper-account")]


async def test_validation_export_downloads_scoped_zip(client, fake_lifecycle):
    await login(client)
    response = await client.get("/api/validation/paper-run-1/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"] == (
        'attachment; filename="bybitbot-validation-paper-run-1.zip"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert fake_lifecycle.calls == [("export", "paper-run-1", "paper", "paper-account")]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("STATUS.md") == b"synthetic validation archive"


@pytest.mark.parametrize(
    ("run_id", "status"),
    [("not-found", 404), ("broken-ledger", 409), ("bad%20id", 422)],
)
async def test_validation_export_maps_errors(client, fake_lifecycle, run_id, status):
    await login(client)
    response = await client.get(f"/api/validation/{run_id}/export")
    assert response.status_code == status
    assert "Traceback" not in response.text
    assert "private" not in response.text


async def test_validation_export_requires_session(client, fake_lifecycle):
    assert (await client.get("/api/validation/paper-run-1/export")).status_code == 401
    assert fake_lifecycle.calls == []


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        (
            "VALIDATION_START",
            {
                "run_id": "paper-run-2",
                "bases": ["BTC", "ETH"],
                "policy": "weak_down",
                "direction": "Put",
                "protocol": {"study_days": 5, "target_entry_days": 21},
                "other_run_ids": ["paper-run-1"],
            },
            202,
        ),
        ("VALIDATION_STOP", {"run_id": "paper-run-1"}, 202),
    ],
)
async def test_validation_commands_enqueue(client, database, kind, payload, expected):
    headers = await login(client)
    body = command(kind, payload)
    responses = await asyncio.gather(
        *(client.post("/api/commands", json=body, headers=headers) for _ in range(2))
    )
    assert {response.status_code for response in responses} == {expected}
    assert sum(response.json()["created"] for response in responses) == 1
    assert responses[0].json()["command"]["payload"] == payload
    assert responses[0].json()["command"]["status"] == "PENDING"
    async with database.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(ControlCommand)) == 1
        assert await session.scalar(select(func.count()).select_from(TradeIntent)) == 0
        assert await session.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("VALIDATION_START", {"run_id": "bad id", "bases": ["BTC"], "policy": "weak_down",
                              "direction": "Put", "protocol": {"study_days": 5}}),
        ("VALIDATION_START", {"run_id": "r1", "bases": ["BTC", "BTC"], "policy": "weak_down",
                              "direction": "Put", "protocol": {"study_days": 5}}),
        ("VALIDATION_START", {"run_id": "r1", "bases": ["BTC"], "policy": "weak_down",
                              "direction": "Call", "protocol": {"study_days": 5}}),
        ("VALIDATION_START", {"run_id": "r1", "bases": ["BTC"], "policy": "weak_down",
                              "direction": "Put", "protocol": {"api_key": "private"}}),
        ("VALIDATION_START", {"run_id": "r1", "bases": ["BTC"], "policy": "weak_down",
                              "direction": "Put", "protocol": {"study_days": 5},
                              "exchange_order": {"symbol": "BTCUSDT"}}),
        ("VALIDATION_STOP", {"run_id": "../../etc/passwd"}),
        ("VALIDATION_STOP", {"run_id": "r1", "api_key": "private"}),
        ("VALIDATION_EXPORT", {"run_id": "r1"}),
    ],
)
async def test_validation_commands_reject_unsafe_payloads(client, kind, payload):
    headers = await login(client)
    response = await client.post("/api/commands", json=command(kind, payload), headers=headers)
    assert response.status_code == 422
    assert "private" not in response.text
