"""Configuration editing is private, conflict-safe, and never changes execution switches."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import yaml

from bybitbot.settings import writer
from bybitbot.settings.loader import ConfigError, load_config
from bybitbot.settings.models import Environment
from bybitbot.web.app import create_app

ORIGIN = "https://console.example.test"
PASSWORD = "synthetic-test-admin-password"
SECRET = "UNRELATED_SECRET_SENTINEL"
EXAMPLE = Path(__file__).resolve().parents[1] / "config/config.example.yaml"
PAIR_FIELDS = (
    "pair_key",
    "symbol_a",
    "symbol_b",
    "hedge_ratio",
    "z_entry",
    "z_exit",
    "z_stop",
    "max_notional",
    "max_unhedged_notional",
)


def payload(saved):
    return {
        "revision": saved["revision"],
        "shadow": copy.deepcopy(saved["shadow"]),
        "auto": {key: value for key, value in saved["auto"].items() if key != "enabled"},
        "pairs": [{key: pair[key] for key in PAIR_FIELDS} for pair in saved["pairs"]],
    }


def save(path, body):
    changes = {key: value for key, value in body.items() if key != "revision"}
    return writer.write_spread(path, changes, revision=body["revision"])


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    text = EXAMPLE.read_text().replace("CHANGE_ME", SECRET)
    text = text.replace("passive_timeout_ms: 2000", "passive_timeout_ms: 4321")
    text = text.replace("recovery_window_ms: 500", "recovery_window_ms: 789")
    text = text.replace('max_slippage_bps: "50"', 'max_slippage_bps: "37"')
    text = text.replace("strategies:\n", "# KEEP THIS COMMENT EXACTLY\nstrategies:\n")
    path.write_text(text)
    path.chmod(0o644)
    return path


@pytest.fixture(scope="module")
def auth_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("shadow-auth") / "admin.json"
    salt = b"synthetic-shadow-config-test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 600_000)
    path.write_text(
        json.dumps(
            {
                "username": "operator",
                "password_hash": f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}",
                "session_secret": "synthetic-shadow-config-session-secret-at-least-32-characters",
            }
        )
    )
    path.chmod(0o600)
    return path


def application(path, auth, database=None, config=None):
    return create_app(
        config or load_config(path),
        database or SimpleNamespace(sessionmaker=None),
        auth,
        config_path=path,
        clock=lambda: dt.datetime(2026, 11, 22, tzinfo=dt.UTC),
    )


async def login(client):
    response = await client.post(
        "/api/login",
        json={"username": "operator", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


@pytest_asyncio.fixture
async def browser(config_path, auth_path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(config_path, auth_path)), base_url=ORIGIN
    ) as client:
        yield client


async def test_auth_origin_and_csrf(browser):
    assert (await browser.get("/api/shadow/config")).status_code == 401
    assert (await browser.put("/api/shadow/config", json={})).status_code == 401
    headers = await login(browser)
    body = payload((await browser.get("/api/shadow/config")).json())
    for invalid_headers in (
        {},
        {"Origin": ORIGIN},
        {"X-CSRF-Token": headers["X-CSRF-Token"]},
        {**headers, "Origin": "https://attacker.example.test"},
        {**headers, "Sec-Fetch-Site": "cross-site"},
        {**headers, "X-CSRF-Token": "incorrect"},
    ):
        response = await browser.put("/api/shadow/config", json=body, headers=invalid_headers)
        assert response.status_code == 403


async def test_save_then_get_without_restart(browser, config_path):
    headers = await login(browser)
    first = (await browser.get("/api/shadow/config")).json()
    assert first["writable"] is True
    assert first["poll_seconds"] == 5
    assert first["pairs"][0]["passive_timeout_ms"] == 4321
    assert SECRET not in json.dumps(first)
    assert "config_path" not in first
    body = payload(first)
    body["shadow"]["enabled"] = True
    body["auto"]["window"] = 80
    body["auto"]["target_notional"] = "125.50"
    response = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["written"] is True
    assert result["restart_required"] is True
    assert "手动重启 worker" in result["note"]
    assert Path(result["backup"]).name == result["backup"]
    assert SECRET not in response.text
    assert str(config_path.parent) not in response.text
    second = (await browser.get("/api/shadow/config")).json()
    assert second["revision"] == result["revision"] != first["revision"]
    assert second["shadow"]["enabled"] is True
    assert second["auto"]["window"] == 80
    assert second["auto"]["target_notional"] == "125.50"
    assert second["auto"]["enabled"] is False
    conflict = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shadow.observation_days", 0),
        ("shadow.observation_days", 31),
        ("shadow.min_depth_notional", "0"),
        ("shadow.max_drawdown", "1.1"),
        ("shadow.min_sharpe", "NaN"),
        ("shadow.impact_bps_per_depth", "Infinity"),
        ("shadow.max_signal_age_ms", 999),
        ("shadow.enabled", "true"),
        ("shadow.public_market_url", "https://attacker.example.test"),
        ("auto.window", 4),
        ("auto.window", 501),
        ("auto.window", 5.5),
        ("auto.sample_interval_seconds", 2),
        ("auto.sample_interval_seconds", 3601),
        ("auto.fee_rate", "1.1"),
        ("auto.fee_rate", 0.001),
        ("auto.slippage_bps", "-1"),
        ("auto.target_notional", "0"),
        ("auto.stop_cooldown_seconds", 59),
        ("auto.max_signal_age_ms", 999),
        ("pairs.0.z_entry", "0.1"),
        ("pairs.0.z_stop", "NaN"),
        ("pairs.0.hedge_ratio", "0"),
        ("pairs.0.pair_key", "WRONG:KEY"),
        ("pairs.0.max_notional.value", "NaN"),
        ("pairs.0.max_unhedged_notional.value", "300"),
    ],
)
async def test_invalid_fields_leave_original_untouched(browser, config_path, field, value):
    headers = await login(browser)
    body = payload((await browser.get("/api/shadow/config")).json())
    original = config_path.read_bytes()
    target = body
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[parts[-1]] = value
    response = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert SECRET not in response.text
    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob("*.bak-shadow-*"))
    assert not list(config_path.parent.glob(".shadow-*"))


@pytest.mark.parametrize("field", ["spread_enabled", "enabled", "auto.enabled", "pairs.0.enabled"])
async def test_execution_switches_rejected(browser, field):
    headers = await login(browser)
    body = payload((await browser.get("/api/shadow/config")).json())
    if field == "auto.enabled":
        body["auto"]["enabled"] = True
    elif field == "pairs.0.enabled":
        body["pairs"][0]["enabled"] = True
    else:
        body[field] = True
    assert (await browser.put("/api/shadow/config", json=body, headers=headers)).status_code == 422


async def test_enabled_requires_pair_and_pair_keys_are_unique(browser):
    headers = await login(browser)
    body = payload((await browser.get("/api/shadow/config")).json())
    duplicate = copy.deepcopy(body)
    duplicate["pairs"] *= 2
    assert (
        await browser.put("/api/shadow/config", json=duplicate, headers=headers)
    ).status_code == 422
    body["pairs"] = []
    body["shadow"]["enabled"] = True
    response = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert response.status_code == 422
    assert "shadow.enabled" in response.text


async def test_json_duplicate_keys_and_raw_nan_rejected(browser):
    headers = {**await login(browser), "Content-Type": "application/json"}
    body = payload((await browser.get("/api/shadow/config")).json())
    duplicate = json.dumps(body).replace('"window": 60', '"window": 60, "window": 70')
    assert (
        await browser.put("/api/shadow/config", content=duplicate, headers=headers)
    ).status_code == 422
    invalid = json.dumps(body).replace('"min_sharpe": "2"', '"min_sharpe": NaN')
    assert (
        await browser.put("/api/shadow/config", content=invalid, headers=headers)
    ).status_code == 422


@pytest.mark.parametrize("execution", ["spread", "auto"])
async def test_execution_enabled_saved_file_read_only(browser, config_path, execution):
    headers = await login(browser)
    raw = yaml.safe_load(config_path.read_text())
    raw["spread"]["pairs"][0]["enabled"] = True
    if execution == "spread":
        raw["spread"]["enabled"] = True
    else:
        raw["spread"]["auto"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(raw))
    result = (await browser.get("/api/shadow/config")).json()
    assert result["writable"] is False
    assert "enabled" in result["read_only_reason"]
    response = await browser.put("/api/shadow/config", json=payload(result), headers=headers)
    assert response.status_code == 403


async def test_startup_execution_is_also_read_only(config_path, auth_path):
    cfg = load_config(config_path)
    cfg = cfg.model_copy(update={"spread": cfg.spread.model_copy(update={"enabled": True})})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(config_path, auth_path, config=cfg)),
        base_url=ORIGIN,
    ) as client:
        headers = await login(client)
        result = (await client.get("/api/shadow/config")).json()
        assert result["writable"] is False
        assert (
            await client.put("/api/shadow/config", json=payload(result), headers=headers)
        ).status_code == 403


async def test_effective_environment_must_be_paper(config_path, auth_path):
    config = load_config(config_path, env_override=Environment.TESTNET)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(config_path, auth_path, config=config)),
        base_url=ORIGIN,
    ) as client:
        headers = await login(client)
        result = (await client.get("/api/shadow/config")).json()
        assert result["writable"] is False
        assert "paper" in result["read_only_reason"]
        assert (
            await client.put("/api/shadow/config", json=payload(result), headers=headers)
        ).status_code == 403


async def test_metrics_separate_saved_and_web_start_config(config_path, auth_path, database):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(config_path, auth_path, database)),
        base_url=ORIGIN,
    ) as client:
        headers = await login(client)
        body = payload((await client.get("/api/shadow/config")).json())
        body["shadow"]["enabled"] = True
        assert (
            await client.put("/api/shadow/config", json=body, headers=headers)
        ).status_code == 200
        result = (await client.get("/api/shadow")).json()
        assert result["enabled"] is False
        assert result["saved_enabled"] is True
        assert result["pending_restart"] is True
        assert result["configuration_source"] == "web_start"
        assert result["runtime_verified"] is False


def test_preserves_unedited_fields_text_and_private_backup(config_path, monkeypatch):
    original = config_path.read_bytes()
    old = load_config(config_path)
    body = payload(writer.read_shadow_config(config_path).public())
    new_pair = copy.deepcopy(body["pairs"][0])
    new_pair.update(pair_key="ETHUSDT:BTCUSDT", symbol_a="ETHUSDT", symbol_b="BTCUSDT")
    body["pairs"].append(new_pair)
    body["shadow"]["enabled"] = True
    actual_replace = writer.os.replace

    def checked_replace(source, target):
        assert Path(target).read_bytes() == original
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o644
        load_config(source)
        actual_replace(source, target)

    monkeypatch.setattr(writer.os, "replace", checked_replace)
    result = save(config_path, body)
    new = load_config(config_path)
    assert new.spread.pairs[0] == old.spread.pairs[0]
    assert new.spread.pairs[1].enabled is False
    for field in (
        "enabled",
        "max_concurrent_pairs",
        "reconciliation_interval_seconds",
        "max_pair_equity_ratio",
        "max_account_leverage",
        "auto",
    ):
        assert getattr(new.spread, field) == getattr(old.spread, field)
    before, _ = original.split(b"spread:\n", 1)
    tail = original[original.index(("\n# " + "-" * 74 + "\n# 策略").encode()) :]
    assert config_path.read_bytes().startswith(before)
    assert config_path.read_bytes().endswith(tail)
    backup = config_path.parent / result["backup"]
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o644
    assert (
        stat.S_IMODE(config_path.with_name(config_path.name + ".shadow.lock").stat().st_mode)
        == 0o600
    )
    assert not list(config_path.parent.glob(".shadow-*"))


def test_existing_pair_execution_flag_preserved(config_path):
    text = config_path.read_text().replace("      enabled: false\n", "      enabled: true\n")
    config_path.write_text(text)
    body = payload(writer.read_shadow_config(config_path).public())
    body["pairs"][0]["z_entry"] = "2.5"
    save(config_path, body)
    pair = load_config(config_path).spread.pairs[0]
    assert pair.enabled is True
    assert str(pair.z_entry) == "2.5"
    assert pair.passive_timeout_ms == 4321


def test_repeated_backups_are_unique(config_path):
    backups = []
    for _ in range(3):
        body = payload(writer.read_shadow_config(config_path).public())
        backups.append(save(config_path, body)["backup"])
    assert len(set(backups)) == 3
    assert all((config_path.parent / backup).is_file() for backup in backups)


def test_writers_serialize_with_revision_conflict(config_path):
    body = payload(writer.read_shadow_config(config_path).public())
    barrier = threading.Barrier(2)

    def attempt(window):
        candidate = copy.deepcopy(body)
        candidate["auto"]["window"] = window
        barrier.wait(timeout=5)
        try:
            return save(config_path, candidate)
        except writer.RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [80, 90]))
    assert results.count("conflict") == 1
    assert len(list(config_path.parent.glob("*.bak-shadow-*"))) == 1
    assert load_config(config_path).spread.auto.window in (80, 90)


def test_full_validation_failure_never_installs_candidate(config_path, monkeypatch):
    body = payload(writer.read_shadow_config(config_path).public())
    original = config_path.read_bytes()
    actual_load = writer.load_config

    def fail_candidate(path, **kwargs):
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
        if Path(path).name.startswith(".shadow-write-"):
            raise ConfigError(f"DSN password={SECRET} at {config_path}")
        return actual_load(path, **kwargs)

    monkeypatch.setattr(writer, "load_config", fail_candidate)
    with pytest.raises(writer.ShadowConfigError) as caught:
        save(config_path, body)
    assert SECRET not in str(caught.value)
    assert str(config_path) not in str(caught.value)
    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob("*.bak-shadow-*"))
    assert not list(config_path.parent.glob(".shadow-*"))


@pytest.mark.parametrize("mutation", ["duplicate", "alias", "symlink", "flow"])
def test_unsafe_yaml_and_symlinks_rejected(config_path, mutation):
    if mutation == "duplicate":
        config_path.write_text(config_path.read_text() + "\nspread: {}\n")
    elif mutation == "alias":
        config_path.write_text(config_path.read_text().replace("window: 60", "window: &window 60"))
    elif mutation == "symlink":
        real = config_path.with_name("real.yaml")
        config_path.rename(real)
        config_path.symlink_to(real)
    else:
        config_path.write_text(
            yaml.safe_dump(yaml.safe_load(config_path.read_text()), default_flow_style=True)
        )
    with pytest.raises((writer.ShadowConfigError, OSError)):
        writer.read_shadow_config(config_path)


async def test_unrelated_config_validation_error_never_leaks(browser, config_path):
    await login(browser)
    config_path.write_text(
        config_path.read_text().replace(
            f"postgresql+psycopg://bybitbot:{SECRET}@127.0.0.1:55432/bybitbot_paper",
            f"{{password: {SECRET}}}",
        )
    )
    response = await browser.get("/api/shadow/config")
    assert response.status_code == 422
    assert SECRET not in response.text
    assert str(config_path) not in response.text


async def test_secret_as_unknown_request_field_is_not_echoed(browser):
    headers = await login(browser)
    body = payload((await browser.get("/api/shadow/config")).json())
    body[SECRET] = SECRET
    response = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert response.status_code == 422
    assert SECRET not in response.text


def test_adds_missing_spread_before_yaml_document_end(config_path):
    text = config_path.read_text()
    _, _, span = writer._document(text.encode())
    config_path.write_text(text[: span[0]] + text[span[1] :] + "\n...\n# END COMMENT\n")
    original = config_path.read_text()
    body = payload(writer.read_shadow_config(config_path).public())
    save(config_path, body)
    new = config_path.read_text()
    assert new[: new.index("spread:\n")] == original[: original.index("...\n")]
    assert new.endswith("...\n# END COMMENT\n")
    load_config(config_path)


async def test_saved_environment_change_makes_editor_read_only(browser, config_path):
    headers = await login(browser)
    config_path.write_text(
        config_path.read_text().replace("active_environment: paper", "active_environment: testnet")
    )
    saved = (await browser.get("/api/shadow/config")).json()
    assert saved["writable"] is False
    assert (
        await browser.put("/api/shadow/config", json=payload(saved), headers=headers)
    ).status_code == 403


async def test_environment_variable_override_is_honored(browser, monkeypatch):
    headers = await login(browser)
    monkeypatch.setenv("BYBITBOT_ENV", "testnet")
    saved = (await browser.get("/api/shadow/config")).json()
    assert saved["writable"] is False
    assert (
        await browser.put("/api/shadow/config", json=payload(saved), headers=headers)
    ).status_code == 403


def test_lock_symlink_rejected(config_path, tmp_path):
    body = payload(writer.read_shadow_config(config_path).public())
    target = tmp_path / "unrelated"
    target.write_text(SECRET)
    lock = config_path.with_name(config_path.name + ".shadow.lock")
    lock.symlink_to(target)
    with pytest.raises(OSError):
        save(config_path, body)
    assert target.read_text() == SECRET


def test_failed_replace_cleans_temporary_files(config_path, monkeypatch):
    original = config_path.read_bytes()
    body = payload(writer.read_shadow_config(config_path).public())

    def failed_replace(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(writer.os, "replace", failed_replace)
    with pytest.raises(OSError):
        save(config_path, body)
    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob(".shadow-*"))
    assert not list(config_path.parent.glob("*.bak-shadow-*"))


def test_crlf_and_trailing_comments_remain_byte_exact(config_path):
    original = config_path.read_bytes().replace(b"\n", b"\r\n")
    config_path.write_bytes(original)
    body = payload(writer.read_shadow_config(config_path).public())
    save(config_path, body)
    updated = config_path.read_bytes()
    assert updated.startswith(original[: original.index(b"spread:\r\n")])
    tail = original[original.index(b"# KEEP THIS COMMENT EXACTLY") :]
    assert updated.endswith(tail)


def test_unwritable_directory_is_readable_but_marked_read_only(config_path, monkeypatch):
    monkeypatch.setattr(writer.os, "access", lambda *args: False)
    saved = writer.read_shadow_config(config_path).public()
    assert saved["writable"] is False
    assert "不可写" in saved["read_only_reason"]


async def test_validation_errors_name_the_failing_field(browser, config_path):
    headers = await login(browser)
    body = payload((await browser.get("/api/shadow/config")).json())
    body["shadow"]["observation_days"] = 0
    response = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert response.status_code == 422
    text = response.text
    assert "shadow.observation_days" in text
    assert "1" in text and "30" in text
    assert SECRET not in text
    assert config_path.name not in text

    body = payload((await browser.get("/api/shadow/config")).json())
    body["pairs"][0]["z_entry"] = "0.1"
    body["pairs"][0]["z_exit"] = "1.0"
    response = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert response.status_code == 422
    assert "pairs[0].z_exit" in response.text
    assert "z_exit < z_entry < z_stop" in response.text
    assert "0.1" not in response.text
    assert "1.0" not in response.text


async def test_field_error_line_is_single_line(browser):
    headers = await login(browser)
    body = payload((await browser.get("/api/shadow/config")).json())
    body["auto"]["window"] = 4
    response = await browser.put("/api/shadow/config", json=body, headers=headers)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "\n" not in detail
    assert "auto.window" in detail


def test_writer_reports_field_paths(config_path):
    body = payload(writer.read_shadow_config(config_path).public())
    body["pairs"][0]["z_exit"] = "9"
    with pytest.raises(writer.ShadowConfigError) as caught:
        save(config_path, body)
    message = str(caught.value)
    assert "pairs[0].z_exit" in message
    assert "z_exit < z_entry < z_stop" in message
    assert "9" not in message


def test_existing_config_mode_is_preserved(config_path):
    config_path.chmod(0o640)
    body = payload(writer.read_shadow_config(config_path).public())
    save(config_path, body)
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
