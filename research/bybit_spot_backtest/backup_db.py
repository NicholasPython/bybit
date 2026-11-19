#!/usr/bin/env python3
"""Create and verify a consistent SQLite snapshot while collection continues."""

import argparse
import datetime as dt
import fcntl
import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def backup(database, destination, retain=14):
    os.umask(0o077)
    if not database.is_file():
        raise FileNotFoundError(database)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination / ".backup.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        free = shutil.disk_usage(destination).free
        if free < database.stat().st_size * 2 + 512 * 1024 * 1024:
            raise RuntimeError("Not enough free space for a verified backup")
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        prefix = database.stem
        final = destination / f"{prefix}-{stamp}.sqlite3"
        temporary = destination / f".{final.name}.tmp"
        if final.exists() or temporary.exists():
            raise FileExistsError(final)
        try:
            with closing(
                sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
            ) as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target, pages=1000, sleep=0.2)
                result = target.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"Backup integrity check failed: {result}")
            with temporary.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(temporary, final)
            digest = hashlib.sha256()
            with final.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(block)
            checksum = final.with_suffix(final.suffix + ".sha256")
            checksum.write_text(f"{digest.hexdigest()}  {final.name}\n", encoding="ascii")
            for old in sorted(destination.glob(prefix + "-????????T??????Z.sqlite3"), reverse=True)[
                retain:
            ]:
                old.unlink()
                old.with_suffix(old.suffix + ".sha256").unlink(missing_ok=True)
            return final, digest.hexdigest()
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "bybit_spot_1m.sqlite3")
    parser.add_argument("--dest", type=Path, default=ROOT / "backups")
    parser.add_argument("--retain", type=int, default=14)
    args = parser.parse_args()
    if args.retain < 1 or args.retain > 365:
        parser.error("--retain must be between 1 and 365")
    path, digest = backup(args.db, args.dest, args.retain)
    print(f"backup={path} sha256={digest}")


if __name__ == "__main__":
    main()
