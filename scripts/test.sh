#!/usr/bin/env bash
# Run the backend tests against a throwaway PostgreSQL that no other checkout uses.
#
# Each run gets its own container name and a free port on 127.0.0.1, so several worktrees (the loop, a child
# session, the user's own checkout) can test at the same time without touching each other's databases.
# Extra arguments go to pytest. Skipped tests fail the run, as in CI.
set -euo pipefail

cd "$(dirname "$0")/../backend"

name="sounding-test-$(date +%s)-$$"
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "$name" -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=sounding_test \
  -p "127.0.0.1:${port}:5432" postgres:16 >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$name" pg_isready -U postgres -d sounding_test >/dev/null 2>&1; then break; fi
  sleep 1
done

log="$(mktemp)"
status=0
TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:${port}/sounding_test" \
  uv run pytest -q -rs -p no:cacheprovider "$@" | tee "$log" || status=$?
if [ "$status" -eq 0 ] && grep -qi "skipped" "$log"; then
  echo "error: tests were skipped (integration tests must run)" >&2
  status=1
fi
rm -f "$log"
exit "$status"
