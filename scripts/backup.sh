#!/usr/bin/env bash
# Back up Sounding: the PostgreSQL database and config/profile.yaml.
#
#   scripts/backup.sh                 # -> ${BACKUP_DIR:-./backups}/<UTC timestamp>/
#   BACKUP_DIR=/mnt/nas/sounding BACKUP_KEEP=30 scripts/backup.sh
#
# Each snapshot contains:
#   db.dump        pg_dump custom format (restore with scripts/restore.sh)
#   profile.yaml   copy of config/profile.yaml (if present)
#   MANIFEST       creation time and row counts, used by `scripts/restore.sh --check`
# The newest BACKUP_KEEP (default 14) snapshots are kept; older ones are deleted.
#
# Not included: .env (keep the secrets, e.g. WEB_DB_PASSWORD and the LLM key, somewhere
# safe yourself).
# Put BACKUP_DIR on another disk or machine; a backup on the same disk is not a backup.
# The stack must be running (the dump is taken from the sounding-db service).
set -euo pipefail

die() { printf ' ✗ %s\n' "$*" >&2; exit 1; }
ok()  { printf ' ✓ %s\n' "$*"; }

cd "$(dirname "$0")/.."
docker compose ps --status running --services 2>/dev/null | grep -qx sounding-db \
  || die "sounding-db is not running (docker compose up -d first)."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"
case "$BACKUP_KEEP" in ''|*[!0-9]*|0) die "BACKUP_KEEP must be a positive integer." ;; esac

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest="$BACKUP_DIR/$stamp"
tmp="$BACKUP_DIR/.$stamp.partial"
[ ! -e "$dest" ] || die "snapshot $dest already exists (run again in a second)."
rm -rf "$tmp"
mkdir -p "$tmp"
trap 'rm -rf "$tmp"' EXIT
umask 077

COUNTS_SQL="select 'sources', count(*) from sources union all
            select 'items', count(*) from items union all
            select 'conversations', count(*) from conversations union all
            select 'conversation_states', count(*) from conversation_states union all
            select 'events', count(*) from events union all
            select 'digests', count(*) from digests union all
            select 'deliveries', count(*) from deliveries union all
            select 'llm_calls', count(*) from llm_calls"

# One snapshot-consistent transaction would be ideal; pg_dump is consistent on its own, and the
# counts are taken right before it. A write in between (the worker) can make --check differ by a
# few rows; rerun the backup in that case.
counts="$(docker compose exec -T sounding-db sh -c \
  "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -At -F= -c \"$COUNTS_SQL\"" | tr -d '\r')"
[ -n "$counts" ] || die "could not count rows."

docker compose exec -T sounding-db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$tmp/db.dump"
[ -s "$tmp/db.dump" ] || die "pg_dump produced an empty file."
ok "database dumped ($(du -h "$tmp/db.dump" | cut -f1))"

if [ -f config/profile.yaml ]; then
  cp config/profile.yaml "$tmp/profile.yaml"
  ok "config/profile.yaml copied"
fi

{
  printf 'created=%s\n' "$stamp"
  printf '%s\n' "$counts"
} > "$tmp/MANIFEST"

mv "$tmp" "$dest"
trap - EXIT
ok "snapshot written: $dest"

# Retention: delete all but the newest BACKUP_KEEP snapshots.
n=0
# Snapshot names are UTC timestamps (no spaces), so a plain ls is safe here.
# shellcheck disable=SC2012
for d in $(ls -1d "$BACKUP_DIR"/2*Z 2>/dev/null | sort -r); do
  n=$((n + 1))
  if [ "$n" -gt "$BACKUP_KEEP" ]; then
    rm -rf "$d"
    ok "removed old snapshot $d"
  fi
done
