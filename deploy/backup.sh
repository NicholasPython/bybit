#!/bin/bash
set -euo pipefail
umask 077
backup_dir="$HOME/.local/share/bybitbot/backups"
config_dir="$HOME/.config/bybitbot"
install -d -m 700 "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$backup_dir/bybitbot-$stamp.dump.gpg"
trap 'rm -f "$target.partial"' EXIT
podman exec bybitbot-pg pg_dump -U bybitbot -d bybitbot_paper -Fc |
  gpg --batch --yes --trust-model always --recipient-file "$config_dir/backup-public.asc" \
      --encrypt --output "$target.partial"
mv "$target.partial" "$target"
printf '%s\n' "$target"
