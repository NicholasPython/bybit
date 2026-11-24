"""Configuration boundaries for the isolated stage-two paper worker."""

from pathlib import Path

import pytest
import yaml

from bybitbot.settings.loader import ConfigError, load_config


@pytest.mark.parametrize("change", ["testnet", "live", "legacy", "non_usdt"])
def test_spread_config_rejects_unvalidated_account_modes(tmp_path, change):
    raw = yaml.safe_load(Path("config/config.example.yaml").read_text())
    raw["spread"]["enabled"] = True
    raw["spread"]["pairs"][0]["enabled"] = True
    if change in {"testnet", "live"}:
        raw["active_environment"] = change
    elif change == "legacy":
        raw["strategies"][0]["enabled"] = True
    else:
        raw["paper"]["initial_balances"]["BTC"] = "1"
    target = tmp_path / "spread.yaml"
    target.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="阶段二套利|不能与"):
        load_config(target)


def test_spread_can_be_enabled_for_a_clean_usdt_paper_account(tmp_path):
    raw = yaml.safe_load(Path("config/config.example.yaml").read_text())
    raw["spread"]["enabled"] = True
    raw["spread"]["pairs"][0]["enabled"] = True
    target = tmp_path / "spread.yaml"
    target.write_text(yaml.safe_dump(raw))
    assert load_config(target).spread.enabled
