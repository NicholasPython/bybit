"""影子盘只读 API 与页面标记测试。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest_asyncio

from bybitbot.settings.loader import load_config
from bybitbot.spread.models import ShadowTrade
from bybitbot.web.app import create_app

ORIGIN = "https://console.example.test"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/config.example.yaml"
PASSWORD = "synthetic-test-admin-password"


@pytest_asyncio.fixture
async def client(database, tmp_path):
    salt = b"synthetic-auth-salt-for-tests"
    digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 600_000)
    auth_path = tmp_path / "admin.json"
    auth_path.write_text(
        json.dumps(
            {
                "username": "operator",
                "password_hash": f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}",
                "session_secret": "synthetic-session-secret-with-more-than-thirty-two-characters",
            }
        )
    )
    auth_path.chmod(0o600)
    config = load_config(CONFIG_PATH)
    moment = dt.datetime(2026, 10, 31, 8, tzinfo=dt.UTC)
    app = create_app(config, database, auth_path, clock=lambda: moment)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as browser:
        yield browser


async def _login(client):
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": "synthetic-test-admin-password"},
        headers={"Origin": "https://console.example.test"},
    )
    assert response.status_code == 200, response.text
    return {"Origin": "https://console.example.test", "X-CSRF-Token": response.json()["csrf_token"]}


async def test_shadow_api_requires_auth_and_returns_readonly_projection(client):
    assert (await client.get("/api/shadow")).status_code == 401
    await _login(client)
    response = await client.get("/api/shadow")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["observation_days"] == 7
    assert body["promotion_candidate"] is False
    assert "不自动改参数" in body["promotion_note"]


async def test_shadow_api_returns_persisted_metrics(client, database):
    async with database.sessionmaker() as session:
        session.add(
            ShadowTrade(
                environment="shadow",
                pair_trade_id="shadow-test",
                direction="",
                side_a="",
                side_b="",
                pair_key="BTCUSDT:ETHUSDT",
                symbol_a="BTCUSDT",
                symbol_b="ETHUSDT",
                decision_at_ms=1_700_000_000_000,
                action="CLOSE",
                reason="EXIT_THRESHOLD_REACHED",
                net_pnl=D("1.25"),
                cumulative_pnl=D("1.25"),
                context={"simulated": True},
            )
        )
        await session.commit()
    await _login(client)
    body = (await client.get("/api/shadow")).json()
    assert body["closed_trades"] == 0  # observation window excludes the fixed old timestamp
    assert body["decisions"] == 0


async def test_console_markup_exposes_shadow_tab(client):
    await _login(client)
    response = await client.get("/")
    assert response.status_code == 200
    assert 'data-tab="shadow"' in response.text
    assert 'id="panel-shadow"' in response.text
    assert "FR-07.3" in response.text
