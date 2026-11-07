#!/bin/sh
# Phase-0 spike: fetch the current and previous month mbox of one IETF list via rsync.
# Run twice; the second run's --stats shows the incremental transfer.
# Works with macOS openrsync (protocol 29): only -t and --stats are used.
set -eu
LIST="${1:-sidrops}"
DIR="$(cd "$(dirname "$0")" && pwd)/data/rsync"
mkdir -p "$DIR"
CUR="$(date +%Y-%m)"
# previous month (BSD date first, GNU date fallback)
PREV="$(date -v-1m +%Y-%m 2>/dev/null || date -d 'last month' +%Y-%m)"
LOG="$DIR/rsync_runs.txt"
for run in 1 2; do
  echo "=== run $run $(date -u +%FT%TZ)" >> "$LOG"
  for M in "$PREV" "$CUR"; do
    rsync -t --stats "rsync.ietf.org::mailman-archive/$LIST/$M.mail" "$DIR/" >> "$LOG" 2>&1 || echo "rsync failed for $M" >> "$LOG"
    sleep 2
  done
done
grep -E '^=== run|Total transferred|Total bytes (sent|received)|Literal data|Matched data|failed' "$LOG"
