#!/usr/bin/env python3
"""Install the Mac watchdog outside Desktop so launchd can run unattended."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import sys
from pathlib import Path

JOBS = {"nas-heartbeat": 30, "offsite-backup": 1800}


def install(
    directory: Path,
    agents: Path,
    python: Path,
    *,
    health_url: str,
    rsync_source: str | None = None,
) -> list[Path]:
    if not health_url:
        raise ValueError("health_url is required")
    directory = directory.expanduser().resolve()
    agents = agents.expanduser().resolve()
    python = python.expanduser().resolve(strict=True)
    if not os.access(python, os.X_OK):
        raise ValueError("Python interpreter must be executable")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("state", "backups", "logs"):
        (directory / name).mkdir(mode=0o700, exist_ok=True)
        (directory / name).chmod(0o700)
    script = directory / "watch_nas.py"
    shutil.copyfile(Path(__file__).with_name("watch_nas.py"), script)
    script.chmod(0o600)
    paths = []
    for name, interval in JOBS.items():
        label = f"io.bybitbot.{name}"
        arguments = [
            str(python),
            str(script),
            "--state-dir",
            str(directory / "state"),
            "--backup-dir",
            str(directory / "backups"),
        ]
        arguments += ["--health-url", health_url]
        if name == "offsite-backup":
            arguments.append("--sync-backups")
            if rsync_source:
                arguments += ["--rsync-source", rsync_source]
        definition = {
            "Label": label,
            "ProcessType": "Background",
            "ProgramArguments": arguments,
            "RunAtLoad": True,
            "StartInterval": interval,
            "StandardOutPath": str(directory / "logs" / f"{label}.log"),
            "StandardErrorPath": str(directory / "logs" / f"{label}.error.log"),
        }
        path = agents / f"{label}.plist"
        path.write_bytes(plistlib.dumps(definition))
        path.chmod(0o600)
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=Path.home() / ".local/share/bybitbot-monitor"
    )
    parser.add_argument(
        "--launch-agent-dir", type=Path, default=Path.home() / "Library/LaunchAgents"
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable).resolve())
    parser.add_argument(
        "--health-url",
        required=True,
        help="被监控服务的 /health 地址；由部署方提供，不写入仓库",
    )
    parser.add_argument(
        "--rsync-source",
        default=None,
        help="加密备份的 rsync 源，如 nas:.local/share/bybitbot/backups/*.dump.gpg",
    )
    args = parser.parse_args()
    os.umask(0o077)
    for path in install(
        args.directory,
        args.launch_agent_dir,
        args.python,
        health_url=args.health_url,
        rsync_source=args.rsync_source,
    ):
        print(path)
    print("Installed. Bootstrap both jobs with launchctl in your logged-in GUI session.")


if __name__ == "__main__":
    main()
