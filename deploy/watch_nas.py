#!/usr/bin/env python3
"""Mac-side heartbeat and encrypted offsite backup fetch; no exchange access."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

DEFAULT_RSYNC_SOURCE = "nas:.local/share/bybitbot/backups/*.dump.gpg"


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


def newest_backup(destination: Path) -> float | None:
    stamps = []
    for path in destination.glob("bybitbot-*.dump.gpg"):
        try:
            stamp = path.name.removeprefix("bybitbot-").removesuffix(".dump.gpg")
            if path.stat().st_size > 0:
                stamps.append(
                    dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC).timestamp()
                )
        except (OSError, ValueError):
            continue
    return max(stamps) if stamps else None


def check_once(
    state: dict, *, healthy: bool, backup_at: float | None, now: float, send=notify
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
    overdue = backup_at is None or now - backup_at > 86400 or backup_at > now + 300
    if overdue and not state.get("backup_notified"):
        state["backup_notified"] = send(
            "Mac 上没有 24 小时内的加密备份，请检查备份定时器和异机复制。"
        )
    elif not overdue:
        state["backup_notified"] = False
    state.update(
        checked_at=now,
        healthy=healthy,
        consecutive_failures=failures,
        newest_backup_at=backup_at,
        backup_overdue=overdue,
    )
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--rsync-source", default=DEFAULT_RSYNC_SOURCE)
    parser.add_argument("--sync-backups", action="store_true")
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
        backup_at=newest_backup(args.backup_dir),
        now=now,
    )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == "__main__":
    main()
