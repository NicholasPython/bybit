"""Offline paper integration uses a new schema and never external market/account access."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from bybitbot import sandbox
from bybitbot.adapters.synthetic import SyntheticMarket
from bybitbot.db.models import RuntimeState
from bybitbot.db.session import Database
from bybitbot.settings.loader import ConfigError, load_config
from bybitbot.settings.models import PaperConfig

TEMPLATE = Path(__file__).resolve().parents[1] / "config/config.example.yaml"
EXPIRY = dt.datetime(2028, 3, 10, 8, tzinfo=dt.UTC)
DSN = "postgresql+psycopg://postgres:bybitbot@127.0.0.1:55432/bybitbot"


@pytest.mark.parametrize("environment", ["testnet", "live"])
def test_synthetic_market_is_rejected_outside_paper(tmp_path, environment):
    raw = yaml.safe_load(TEMPLATE.read_text())
    raw["active_environment"] = environment
    raw["paper"].update(market_source="synthetic", synthetic_expiry=EXPIRY.isoformat())
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="只允许用于 paper"):
        load_config(path)


@pytest.mark.parametrize(
    "expiry",
    [None, "2028-03-10T08:00:00", "2028-03-10T08:00:01Z", "2028-03-10T09:00:00Z"],
)
def test_synthetic_contract_requires_fixed_aware_delivery_time(expiry):
    with pytest.raises(ValidationError):
        PaperConfig(market_source="synthetic", synthetic_expiry=expiry)


async def test_synthetic_restart_preserves_contract_identity_and_quote_prices():
    first = SyntheticMarket(EXPIRY)
    restarted = SyntheticMarket(EXPIRY)
    first_specs = await first.get_instruments("option")
    second_specs = await restarted.get_instruments("option")
    assert first_specs == second_specs
    assert {spec.delivery_time for spec in first_specs} == {EXPIRY}
    assert {spec.base_coin for spec in first_specs} == {"BTC", "ETH"}
    for spec in first_specs:
        old_quote = await first.get_ticker("option", spec.symbol)
        new_quote = await restarted.get_ticker("option", spec.symbol)
        assert new_quote.bid_price == old_quote.bid_price
        assert new_quote.ask_price == old_quote.ask_price
        assert new_quote.source_ts >= old_quote.source_ts
        book = await restarted.get_orderbook("option", spec.symbol)
        assert book.bids[0][0] == new_quote.bid_price
        assert book.asks[0][0] == new_quote.ask_price


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql+psycopg://postgres:synthetic@db.example.test/bybitbot",
        DSN + "?options=-csearch_path=public",
        DSN + "?host=db.example.test",
        "sqlite+aiosqlite:///test.db",
    ],
)
async def test_initialize_rejects_nonlocal_or_overridden_dsn_before_writes(tmp_path, dsn):
    directory = tmp_path / "refused"
    with pytest.raises(ValueError, match="本机"):
        await sandbox.initialize(directory, dsn=dsn, template=TEMPLATE)
    assert not directory.exists()


@pytest.mark.parametrize("variable", ["BYBITBOT_DB_DSN", "BYBITBOT_ENV"])
async def test_initialize_rejects_environment_overrides_before_writes(
    tmp_path, monkeypatch, variable
):
    monkeypatch.setenv(variable, "synthetic-override")
    directory = tmp_path / "refused"
    with pytest.raises(RuntimeError, match="覆盖隔离配置"):
        await sandbox.initialize(directory, dsn=DSN, template=TEMPLATE)
    assert not directory.exists()


async def test_initialize_refuses_existing_directory_without_db_or_file_writes(
    tmp_path, monkeypatch
):
    directory = tmp_path / "existing"
    directory.mkdir()
    marker = directory / "preserve.txt"
    marker.write_bytes(b"existing evidence")

    def forbidden_engine(*args, **kwargs):
        pytest.fail("Existing directory must be rejected before creating a DB engine")

    monkeypatch.setattr(sandbox, "create_async_engine", forbidden_engine)
    with pytest.raises(FileExistsError):
        await sandbox.initialize(directory, dsn=DSN, template=TEMPLATE)
    assert list(directory.iterdir()) == [marker]
    assert marker.read_bytes() == b"existing evidence"


async def test_real_migrations_acceptance_and_repeat_preserve_existing_ledger(
    tmp_path, monkeypatch
):
    # This owns only the schema returned by this call; no pre-existing sandbox is inspected.
    directory = tmp_path / "new-system"
    dsn = os.environ.get("BYBITBOT_TEST_DSN", DSN)
    result = await sandbox.initialize(directory, dsn=dsn, template=TEMPLATE)
    schema = result["schema"]
    admin = create_async_engine(dsn, hide_parameters=True)
    database = None
    try:
        config = sandbox.load_sandbox(directory)
        assert config.account.expected_uid == schema
        assert set(config.environments) == {"paper"}
        assert not config.profile.allow_credentials
        assert not config.profile.allow_trade_writes
        assert not config.live_gates.live_allowed
        assert make_url(config.profile.db_dsn).query == {"options": f"-csearch_path={schema}"}
        for name in ("config.yaml", "sandbox.json", "admin.json", "admin-password.txt"):
            assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

        def no_external_adapter(*args, **kwargs):
            pytest.fail("Synthetic worker must not instantiate REST or load exchange credentials")

        monkeypatch.setattr("bybitbot.service.BybitRestAdapter", no_external_adapter)
        monkeypatch.setattr("bybitbot.service.load_credentials", no_external_adapter)
        report = await sandbox.verify(directory)
        assert report["passed"]
        assert report["real_fills_verified"] is False
        assert report["market_source"] == "synthetic"
        assert {
            "scheduled_spot_and_option",
            "next_quote_fill",
            "pause_rejects_new_risk",
            "reduce_while_paused",
            "restart_preserves_balances_and_fills",
            "restart_requires_takeover",
        } <= set(report["checks"])
        assert len(report["executions"]) == 3
        assert all(Decimal(fill["fee"]) > 0 for fill in report["executions"])
        assert all(order["status"] == "FILLED" for order in report["orders"])
        option = next(row for row in report["inventory"] if "-" in row["symbol"])
        assert Decimal(option["qty"]) == 0
        assert json.loads((directory / "verification.json").read_text()) == report

        database = Database(config.profile.db_dsn)
        async with database.sessionmaker() as session:
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        migration_config = Config("alembic.ini")
        assert version == ScriptDirectory.from_config(migration_config).get_current_head()

        async def snapshot():
            async with database.sessionmaker() as session:
                status = await session.scalar(
                    select(RuntimeState).where(RuntimeState.key == "status")
                )
                persisted = (status.value, status.updated_at)
            return persisted, await sandbox._facts(database)

        before = await snapshot()
        with pytest.raises(RuntimeError, match="已有验收记录"):
            await sandbox.verify(directory)
        assert await snapshot() == before
        # A used sandbox without its report must also refuse before service startup.
        (directory / "verification.json").rename(directory / "verification.saved.json")
        with pytest.raises(RuntimeError, match="只允许未交易"):
            await sandbox.verify(directory)
        assert await snapshot() == before
        raw = yaml.safe_load((directory / "config.yaml").read_text())
        raw["environments"]["paper"]["db_dsn"] = dsn
        (directory / "config.yaml").write_text(yaml.safe_dump(raw))
        with pytest.raises(ValueError, match="隔离配置已变更"):
            await sandbox.verify(directory)
        assert await snapshot() == before
    finally:
        if database is not None:
            await database.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
