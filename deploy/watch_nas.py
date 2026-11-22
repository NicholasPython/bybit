#!/usr/bin/env python3
"""Mac-side heartbeat and encrypted offsite backup check; no exchange access."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path

DEFAULT_RSYNC_SOURCE = "nas:.local/share/bybitbot/backups/*.dump.gpg"
#: 要求存在的备份库。实盘库尚未开通时，从这里去掉它。
DEFAULT_EXPECTED_DATABASES = "bybitbot_paper bybitbot_testnet bybitbot_live"
#: pg_dump 自定义格式的文件魔数。解密后必须以它开头，才说明这是一份真备份。
PGDMP_MAGIC = b"PGDMP"
_BACKUP_NAME = re.compile(
    r"^bybitbot-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<database>[A-Za-z0-9_]+))?\.dump\.gpg$"
)
#: 校验结果缓存，键为（路径, 大小, mtime）。同一份文件只解密一次。
_VERIFIED: dict[tuple[str, int, int], bool] = {}


def health_ok(url: str) -> bool:
    if not url:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/curl", "--fail", "--silent", "--max-time", "8", url],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return result.returncode == 0 and json.loads(result.stdout).get("status") == "ok"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def notify(message: str) -> bool:
    # All messages are fixed application text, never shell or exchange input.
    literal = json.dumps(message, ensure_ascii=False)
    try:
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/osascript", "-e", f'display notification {literal} with title "Bybit NAS"'],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sync_backups(destination: Path, source: str = DEFAULT_RSYNC_SOURCE) -> bool:
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    try:
        result = subprocess.run(  # noqa: S603
            [
                "/usr/bin/rsync",
                "-az",
                "--chmod=Fu=rw,Fgo=,Du=rwx,Dgo=",
                "-e",
                (
                    "ssh -o BatchMode=yes -o ConnectTimeout=8 "
                    "-o ServerAliveInterval=5 -o ServerAliveCountMax=2"
                ),
                source,
                str(destination) + "/",
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def parse_backup_name(name: str) -> tuple[dt.datetime | None, str | None]:
    """解析备份文件名，返回（时间戳, 数据库名）。

    老版本文件名没有库名后缀，数据库名为 ``None``。
    """
    match = _BACKUP_NAME.fullmatch(name)
    if match is None:
        return None, None
    try:
        stamp = dt.datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.UTC
        )
    except ValueError:
        return None, None
    return stamp, match.group("database")


def verify_backup(path: Path, gnupg_home: Path | None = None) -> bool:
    """解密并检查 pg_dump 魔数 —— 证明这份备份真的可以被恢复。

    只看文件大小是不够的：截断或损坏的文件同样非空。解密成功同时说明
    密文通过了 AEAD 完整性校验；解密后的内容必须是 pg_dump 归档。
    """
    command = ["gpg", "--batch", "--quiet", "--decrypt"]
    if gnupg_home is not None:
        command += ["--homedir", str(gnupg_home)]
    command.append(str(path))
    try:
        result = subprocess.run(  # noqa: S603
            command, capture_output=True, check=False, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout[: len(PGDMP_MAGIC)] == PGDMP_MAGIC


def backup_inventory(
    directory: Path, *, gnupg_home: Path | None = None, verify: bool = True
) -> dict[str, dict]:
    """每个数据库最新一份备份：库名 -> {at, path, verified}。

    校验结果按（路径, 大小, mtime）缓存，避免每轮检查都重新解密。
    """
    inventory: dict[str, dict] = {}
    if not directory.is_dir():
        return inventory
    for path in sorted(directory.glob("bybitbot-*.dump.gpg")):
        stamp, database = parse_backup_name(path.name)
        if stamp is None or database is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size == 0:
            continue
        current = inventory.get(database)
        if current is not None and current["at"] >= stamp.timestamp():
            continue
        if verify:
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            if key not in _VERIFIED:
                if len(_VERIFIED) > 256:
                    _VERIFIED.clear()
                _VERIFIED[key] = verify_backup(path, gnupg_home)
            verified = _VERIFIED[key]
        else:
            verified = False
        inventory[database] = {"at": stamp.timestamp(), "path": path, "verified": verified}
    return inventory


def backup_problems(
    inventory: dict[str, dict], expected: list[str], now: float, *, max_age: float = 86400
) -> list[str]:
    """逐库核对备份：缺失、过期、校验不通过都要报出来。"""
    problems = []
    for database in expected:
        entry = inventory.get(database)
        if entry is None:
            problems.append(f"{database} 没有备份")
            continue
        if now - entry["at"] > max_age or entry["at"] > now + 300:
            problems.append(f"{database} 备份已过期")
        elif not entry["verified"]:
            problems.append(f"{database} 备份校验不通过")
    return problems


def check_once(
    state: dict,
    *,
    healthy: bool,
    inventory: dict[str, dict],
    expected: list[str],
    now: float,
    send=notify,
) -> dict:
    state = dict(state)
    failures = 0 if healthy else int(state.get("consecutive_failures", 0)) + 1
    was_down = bool(state.get("down_notified"))
    if failures >= 3 and not was_down:
        state["down_notified"] = send(
            "连续三次无法确认交易服务心跳。挂单和持仓状态未知，请核对 NAS。"
        )
    elif healthy and was_down and send("交易服务心跳已恢复；恢复交易仍需原有对账和暂停解除条件。"):
        state["down_notified"] = False
    problems = backup_problems(inventory, expected, now)
    if problems and not state.get("backup_notified"):
        state["backup_notified"] = send(
            "备份异常：" + "；".join(problems) + "。请检查备份定时器和异机复制。"
        )
    elif not problems:
        state["backup_notified"] = False
    state.update(
        checked_at=now,
        healthy=healthy,
        consecutive_failures=failures,
        backup_problems=problems,
    )
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--rsync-source", default=DEFAULT_RSYNC_SOURCE)
    parser.add_argument("--sync-backups", action="store_true")
    parser.add_argument("--gnupg-home", type=Path, default=None)
    parser.add_argument("--expected-databases", default=DEFAULT_EXPECTED_DATABASES)
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.sync_backups:
        ok = sync_backups(args.backup_dir, args.rsync_source)
        result = {"checked_at": time.time(), "backup_sync_ok": ok}
        (args.state_dir / "backup-sync.json").write_text(json.dumps(result) + "\n")
        if not ok:
            raise SystemExit(1)
        return
    path = args.state_dir / "nas-watch.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    now = time.time()
    state = check_once(
        state,
        healthy=health_ok(args.health_url),
        inventory=backup_inventory(args.backup_dir, gnupg_home=args.gnupg_home),
        expected=[name for name in args.expected_databases.split() if name],
        now=now,
    )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == "__main__":
    main()
