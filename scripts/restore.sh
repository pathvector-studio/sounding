#!/usr/bin/env bash
# Restore a Sounding snapshot made by scripts/backup.sh.
#
#   scripts/restore.sh --check <snapshot>   # restore into a throwaway database and compare
#                                           # the row counts with MANIFEST; live data untouched
#   scripts/restore.sh --yes   <snapshot>   # REPLACE the live database with the snapshot
#
# --yes first takes a backup of the current state (scripts/backup.sh), stops sounding-web and
# sounding-worker, drops and recreates the database, restores the dump (including the SELECT
# grants for the read-only role sounding_web, which lives in the cluster and survives), starts
# the worker and web again and verifies the row counts. The worker re-runs the migrations on
# start (forward only). config/profile.yaml and .env are NOT touched; the snapshot's
# profile.yaml is there for you to copy back by hand if you need it.
set -euo pipefail

die() { printf ' ✗ %s\n' "$*" >&2; exit 1; }
ok()  { printf ' ✓ %s\n' "$*"; }

[ $# -eq 2 ] || die "usage: $0 --check|--yes <snapshot-dir>"
mode="$1"
snap="${2%/}"
case "$mode" in --check|--yes) ;; *) die "usage: $0 --check|--yes <snapshot-dir>" ;; esac

cd "$(dirname "$0")/.."
[ -f "$snap/db.dump" ] && [ -f "$snap/MANIFEST" ] || die "$snap is not a backup snapshot."
docker compose ps --status running --services 2>/dev/null | grep -qx sounding-db \
  || die "sounding-db is not running (docker compose up -d sounding-db first)."

db() { docker compose exec -T sounding-db sh -c "$1"; }
COUNTS_SQL="select 'sources', count(*) from sources union all
            select 'items', count(*) from items union all
            select 'conversations', count(*) from conversations union all
            select 'conversation_states', count(*) from conversation_states union all
            select 'events', count(*) from events union all
            select 'digests', count(*) from digests union all
            select 'deliveries', count(*) from deliveries union all
            select 'llm_calls', count(*) from llm_calls"
want="$(grep -E '^(sources|items|conversations|conversation_states|events|digests|deliveries|llm_calls)=' "$snap/MANIFEST")"

# compare_counts <database>: die unless the row counts equal MANIFEST.
compare_counts() {
  local got
  got="$(db "psql -U \"\$POSTGRES_USER\" -d $1 -At -F= -c \"$COUNTS_SQL\"" | tr -d '\r')"
  [ "$got" = "$want" ] || die "row counts differ from MANIFEST:
  restored: $(echo $got)
  manifest: $(echo $want)"
  ok "row counts match MANIFEST ($(echo $got))"
}

if [ "$mode" = "--check" ]; then
  check_db="sounding_restore_check"
  db "dropdb -U \"\$POSTGRES_USER\" --if-exists $check_db && createdb -U \"\$POSTGRES_USER\" $check_db"
  trap 'db "dropdb -U \"\$POSTGRES_USER\" --if-exists '"$check_db"'" >/dev/null 2>&1 || true' EXIT
  db "pg_restore -U \"\$POSTGRES_USER\" -d $check_db --no-owner --exit-on-error" < "$snap/db.dump"
  ok "db.dump restored into $check_db"
  compare_counts "$check_db"
  ok "snapshot $snap is restorable"
  exit 0
fi

# --yes: replace live data.
scripts/backup.sh
ok "current state saved before restoring (see above)"

docker compose stop sounding-web sounding-worker >/dev/null
trap 'docker compose start sounding-worker sounding-web >/dev/null' EXIT
ok "web and worker stopped"

# Drop and recreate rather than --clean: nothing from the live schema (tables added after the
# backup, for example) can be left behind. The dump carries the GRANTs to sounding_web.
db 'dropdb -U "$POSTGRES_USER" --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
db 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < "$snap/db.dump"
ok "database restored"
compare_counts '"$POSTGRES_DB"'

docker compose start sounding-worker sounding-web >/dev/null
trap - EXIT
ok "worker and web started; restored $snap"
