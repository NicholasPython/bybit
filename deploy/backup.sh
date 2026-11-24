#!/bin/bash
# 加密备份各环境数据库。
#
# 每份备份都必须先通过 pg_restore --list 才算可用 —— 导得出来不等于恢复得回来。
# 备份按数据库分文件存放（bybitbot-<stamp>-<db>.dump.gpg），异机监控可以逐库核对，
# 某一份损坏或缺失不会掩盖其他库。
set -euo pipefail
umask 077

backup_dir="$HOME/.local/share/bybitbot/backups"
config_dir="$HOME/.config/bybitbot"
container="${BYBITBOT_PG_CONTAINER:-bybitbot-pg}"
db_user="${BYBITBOT_PG_USER:-bybitbot}"
databases="${BYBITBOT_PG_DATABASES:-bybitbot_paper bybitbot_testnet bybitbot_live}"
retention_days="${BYBITBOT_BACKUP_RETENTION_DAYS:-14}"

install -d -m 700 "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
workdir=$(mktemp -d)
chmod 700 "$workdir"
trap 'rm -rf "$workdir"; rm -f "$backup_dir"/bybitbot-*.dump.gpg.partial' EXIT

# 容器内没有 pg_restore 时退回本地校验 pg_dump 魔数（PGDMP）。
has_pg_restore() {
  podman exec "$container" pg_restore --version >/dev/null 2>&1
}

written=""
skipped=""
failed=""

for db in $databases; do
  # 直接用 pg_dump 的成败判定：库不存在（尚未开通）时跳过，
  # 导出失败则是真失败。不额外依赖容器里有没有 psql。
  if ! podman exec "$container" pg_dump -U "$db_user" -d "$db" -Fc -f "/tmp/$db.dump" \
    2>"$workdir/$db.err"; then
    if grep -q "does not exist" "$workdir/$db.err"; then
      skipped="$skipped $db"
    else
      failed="$failed $db"
    fi
    continue
  fi
  podman cp "$container:/tmp/$db.dump" "$workdir/$db.dump"
  if has_pg_restore; then
    if ! podman exec "$container" pg_restore --list "/tmp/$db.dump" >"$workdir/$db.list" \
      || [ ! -s "$workdir/$db.list" ]; then
      failed="$failed $db"
      podman exec "$container" rm -f "/tmp/$db.dump"
      continue
    fi
  elif ! head -c 5 "$workdir/$db.dump" | grep -q '^PGDMP$'; then
    failed="$failed $db"
    podman exec "$container" rm -f "/tmp/$db.dump"
    continue
  fi
  podman exec "$container" rm -f "/tmp/$db.dump"
  target="$backup_dir/bybitbot-$stamp-$db.dump.gpg"
  gpg --batch --yes --trust-model always --recipient-file "$config_dir/backup-public.asc" \
    --encrypt --output "$target.partial" "$workdir/$db.dump"
  mv "$target.partial" "$target"
  written="$written $db"
done

# 轮转：只保留保留期内的备份，避免备份目录本身把磁盘撑满。
find "$backup_dir" -type f -name 'bybitbot-*.dump.gpg' -mtime "+$retention_days" -delete

if [ -n "$failed" ]; then
  printf '备份失败（导出或可恢复性校验未通过）：%s\n' "$failed" >&2
  exit 1
fi
if [ -z "$written" ]; then
  printf '没有可备份的数据库（库不存在：%s）\n' "$skipped" >&2
  exit 1
fi
printf '已备份：%s（跳过：%s）\n' "$written" "${skipped:-无}"
