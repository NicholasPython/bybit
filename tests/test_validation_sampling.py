"""Sampling identities, recovery gaps and atomic budgets on isolated schemas."""

import asyncio
import datetime as dt
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import func, select

from bybitbot.adapters.bybit_rest import BybitRestAdapter
from bybitbot.adapters.synthetic import SyntheticMarket
from bybitbot.clock import now_utc
from bybitbot.db.models import Order
from bybitbot.service import TradingService
from bybitbot.settings.loader import load_config
from bybitbot.validation import SpreadProtocol
from bybitbot.validation.models import ValidationLedger, ValidationObservation, ValidationPosition
from bybitbot.validation.sampling import ValidationSampler, ValidationStore


async def start(store, **changes):
    values = {
        "run_id": "cohort",
        "bases": ["BTC", "ETH"],
        "policy": "strong_up",
        "direction": "Call",
        "protocol": SpreadProtocol(),
        "started_ms": 1000,
    }
    return await store.start(**(values | changes))


async def test_start_is_idempotent_but_never_rebinds_identity(sessionmaker):
    store = ValidationStore(sessionmaker, "paper", "account")
    runs = await asyncio.gather(start(store), start(store))
    assert runs[0].id == runs[1].id
    with pytest.raises(ValueError, match="另一份"):
        await start(store, started_ms=1001)
    with pytest.raises(ValueError, match="paper"):
        await start(ValidationStore(sessionmaker, "live", "account"))
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(ValidationPosition)) == 2


async def test_two_bases_cannot_double_spend_cohort_budget(sessionmaker):
    store = ValidationStore(sessionmaker, "paper", "account")
    study = await start(store)
    results = await asyncio.gather(
        store.reserve(study.id, "BTC", D(18)), store.reserve(study.id, "ETH", D(18))
    )
    assert sorted(results) == [False, True]
    async with sessionmaker() as session:
        assert await session.scalar(select(func.sum(ValidationPosition.reserved_risk))) == D(18)
    with pytest.raises(ValueError, match="账户"):
        await ValidationStore(sessionmaker, "paper", "other").reserve(study.id, "BTC", D(1))


async def test_sample_slot_retry_restart_and_failure_gap_are_distinct(sessionmaker):
    store = ValidationStore(sessionmaker, "paper", "account")
    study = await start(store)
    assert await store.record(study.id, slot_ms=1000, observed_ms=1000, payload={"value": 1})
    assert not await store.record(study.id, slot_ms=1000, observed_ms=1001, payload={"value": 2})
    study = (await store.active())[0]
    assert study.next_due_ms == 901000
    assert await store.record(
        study.id, slot_ms=901000, observed_ms=901000, payload={"error": "offline"}, error="offline"
    )
    restarted = ValidationStore(sessionmaker, "paper", "account")
    study = (await restarted.active())[0]
    assert study.last_observed_ms == 1000
    assert study.next_due_ms == 1801000
    # Late recovery writes the actual observation, never invents missed snapshots.
    assert await restarted.record(
        study.id, slot_ms=1801000, observed_ms=2701001, payload={"value": 3}
    )
    async with sessionmaker() as session:
        rows = list(
            await session.scalars(select(ValidationObservation).order_by(ValidationObservation.id))
        )
        assert [row.status for row in rows] == ["ok", "error", "ok"]
        gaps = list(
            await session.scalars(
                select(ValidationLedger).where(ValidationLedger.kind == "observation_gap")
            )
        )
        assert {row.payload["base"] for row in gaps} == {"BTC", "ETH"}
        assert all(row.payload["gap_seconds"] == "2700.001" for row in gaps)


async def test_sampler_continues_in_paused_worker_without_orders(database):
    config = load_config("config/config.example.yaml")
    expiry = (now_utc() + dt.timedelta(days=21)).replace(hour=8, minute=0, second=0, microsecond=0)
    service = TradingService(config, database, market_adapter=SyntheticMarket(expiry))
    await service.start()
    try:
        assert not service.machine.allows_new_exposure
        store = ValidationStore(database.sessionmaker, "paper", service.uid)
        moment = int(now_utc().timestamp() * 1000)
        await start(store, started_ms=moment)
        await service.tick()
        await service.tick()
        async with database.sessionmaker() as session:
            rows = list(await session.scalars(select(ValidationObservation)))
            assert len(rows) == 1 and rows[0].status == "ok"
            assert rows[0].payload["bases"]["BTC"]["signal"]["eligible"] is True
            assert await session.scalar(select(func.count()).select_from(Order)) == 0
    finally:
        await service.close()


async def test_failed_sample_has_error_heartbeat_and_no_success_cursor(sessionmaker):
    store = ValidationStore(sessionmaker, "paper", "account")
    await start(store)

    class Offline:
        async def get_server_time(self):
            raise RuntimeError("secret material must never enter the database")

    await ValidationSampler(store, Offline()).tick(now_ms=1000)
    study = (await store.active())[0]
    assert study.last_observed_ms is None
    assert study.last_error == "sample_failed:RuntimeError"
    assert study.heartbeat_ms == 1000


async def test_candle_adapter_uses_only_unsigned_public_get():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "time": 1000,
                "result": {"symbol": "BTCUSDT", "category": "spot", "list": []},
            },
        )

    adapter = BybitRestAdapter(
        base_url="https://api.bybit.com",
        environment="paper",
        api_key="unused",
        api_secret="unused",
        allow_trade_writes=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter._client = client
        assert await adapter.get_klines("BTCUSDT", "60", end_ms=1000, limit=200) == []
        with pytest.raises(ValueError):
            await adapter.get_klines("BTCUSDT", "1", end_ms=1000, limit=200)
        adapter._client = None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET" and request.url.path == "/v5/market/kline"
    assert request.url.params["end"] == "1000"
    assert not any(name.startswith("x-bapi") for name in request.headers)
