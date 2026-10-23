#!/bin/sh
# Run from the deployed project root. Files contain private data: keep them private.
set -eu
backup_root=${1:-/opt/omini-toolbox/backups}
umask 077
mkdir -p "$backup_root"
target="$backup_root/omini-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
temporary="$target.tmp"
dump="$target.sql.tmp"
key="$target.account-sync.key"
memo_key="$target.memo-storage.key"
trap 'rm -f "$temporary" "$dump" "$key.tmp" "$memo_key.tmp"' EXIT HUP INT TERM
docker compose -f deploy/compose.yaml exec -T web python -c 'import os, pathlib, sys; p=pathlib.Path(os.environ.get("OMINI_SYNC_KEY_FILE", "/data/account-sync.key")); sys.stdout.buffer.write(p.read_bytes())' > "$key.tmp"
test -s "$key.tmp"
docker compose -f deploy/compose.yaml exec -T web python -c 'import pathlib, sys; p=pathlib.Path("/data/memo-storage.key"); sys.stdout.buffer.write(p.read_bytes())' > "$memo_key.tmp"
test -s "$memo_key.tmp"
docker compose -f deploy/compose.yaml exec -T mysql sh -c 'MYSQL_PWD="$(cat /run/secrets/mysql_root_password)" exec mysqldump -uroot --single-transaction --no-tablespaces --hex-blob omini' > "$dump"
gzip -c "$dump" > "$temporary"
gzip -t "$temporary"
mv "$temporary" "$target"
mv "$key.tmp" "$key"
mv "$memo_key.tmp" "$memo_key"
printf 'Backup saved: %s\n' "$target"
printf 'Encryption key saved: %s (keep with the database backup)\n' "$key"
printf 'Memo encryption key saved: %s (keep with the database backup)\n' "$memo_key"
