#!/bin/sh
# Phase-0 spike: record monthly mbox file sizes (listing only, nothing is downloaded).
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)/data/rsync"
mkdir -p "$DIR"
for L in sidrops idr grow ietf; do
  rsync --list-only "rsync.ietf.org::mailman-archive/$L/" > "$DIR/list_$L.txt"
  sleep 2
done
wc -l "$DIR"/list_*.txt
