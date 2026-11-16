"""Configuration identity must survive Python process hash randomization."""

import os
import subprocess
import sys
from pathlib import Path

from bybitbot.service import config_fingerprint
from bybitbot.settings.loader import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_config_identity_is_stable_across_hash_seeds():
    script = (
        "from bybitbot.settings.loader import load_config; "
        "from bybitbot.service import config_fingerprint; "
        "print(config_fingerprint(load_config('config/config.example.yaml')))"
    )
    fingerprints = {
        subprocess.check_output(  # noqa: S603 — fixed interpreter/script, no external input
            [sys.executable, "-P", "-c", script],
            cwd=ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        ).strip()
        for seed in ("1", "2", "3", "42")
    }
    assert len(fingerprints) == 1


def test_config_identity_still_changes_for_budget_and_schedule():
    config = load_config(ROOT / "config/config.example.yaml")
    changed = config.model_copy(
        update={"budget": config.budget.model_copy(update={"max_open_orders": 1})}
    )
    assert config_fingerprint(changed) != config_fingerprint(config)
    strategy = config.strategies[0].model_copy(update={"schedule_utc": ("03:21",)})
    changed = config.model_copy(update={"strategies": (strategy, *config.strategies[1:])})
    assert config_fingerprint(changed) != config_fingerprint(config)
