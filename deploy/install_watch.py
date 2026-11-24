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
    gnupg_home: Path | None = None,
    expected_databases: str | None = None,
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
        if gnupg_home is not None:
            # 备份校验要解密才能做；没有私钥环就退化成"只看文件存在"，
            # 那就等于回到了损坏备份也判正常的老问题。
            arguments += ["--gnupg-home", str(gnupg_home)]
        if expected_databases:
            arguments += ["--expected-databases", expected_databases]
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
    parser.add_argument(
        "--gnupg-home",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "secrets" / "backup-gnupg",
        help="备份私钥环目录；用于解密校验异机备份是否真的可以恢复",
    )
    parser.add_argument(
        "--expected-databases",
        default=None,
        help="要求存在的备份库，空格分隔；实盘库尚未开通时从列表中去掉它",
    )
    args = parser.parse_args()
    os.umask(0o077)
    for path in install(
        args.directory,
        args.launch_agent_dir,
        args.python,
        health_url=args.health_url,
        rsync_source=args.rsync_source,
        gnupg_home=args.gnupg_home,
        expected_databases=args.expected_databases,
    ):
        print(path)
    print("Installed. Bootstrap both jobs with launchctl in your logged-in GUI session.")


if __name__ == "__main__":
    main()
