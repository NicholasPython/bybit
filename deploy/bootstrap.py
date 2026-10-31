"""Generate new paper deployment files without exposing passwords in output."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

import yaml

from bybitbot.cli import init_admin


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise SystemExit(
            "Target directory is not empty; refusing to replace deployment credentials"
        )
    password = secrets.token_hex(24)
    config = yaml.safe_load(Path("config/config.example.yaml").read_text())
    config["runtime"]["proxy"] = "http://127.0.0.1:7890"
    config["runtime"]["poll_seconds"] = 10
    config["environments"]["paper"]["db_dsn"] = (
        f"postgresql+psycopg://bybitbot:{password}@127.0.0.1:55432/bybitbot_paper"
    )
    config["ops"]["backup_target"] = "/home/nicholas/.local/share/bybitbot/backups"
    for name, content in {
        "config.yaml": yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        "postgres.env": (
            "POSTGRES_USER=bybitbot\nPOSTGRES_DB=bybitbot_paper\n"
            f"POSTGRES_PASSWORD={password}\n"
        ),
    }.items():
        with open(directory / name, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(content)
    init_admin(
        directory / "admin.json", password_file=directory / "admin-password.txt", username="admin"
    )
    print("Paper config generated; strategies disabled, live gate closed")


if __name__ == "__main__":
    main()
