#!/bin/sh
# Run from the deployed project root. Files contain private data: keep them private.
set -eu
backup_root=${1:-/opt/omini-toolbox/backups}
umask 077
mkdir -p "$backup_root"
target="$backup_root/omini-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
temporary="$target.tmp"
dump="$target.sql.tmp"
trap 'rm -f "$temporary" "$dump"' EXIT HUP INT TERM
docker compose -f deploy/compose.yaml exec -T mysql sh -c 'MYSQL_PWD="$(cat /run/secrets/mysql_root_password)" exec mysqldump -uroot --single-transaction --no-tablespaces --hex-blob omini' > "$dump"
gzip -c "$dump" > "$temporary"
gzip -t "$temporary"
mv "$temporary" "$target"
printf 'Backup saved: %s\n' "$target"
