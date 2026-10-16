#!/usr/bin/env bash
# Measure the API container's memory under Render-free-like limits (512 MB, embedded worker,
# migrations on start, production settings), with and without the semantic embedder.
#
#   docker build -f infra/docker/api.Dockerfile -t sf-api:base .
#   docker build -f infra/docker/api.Dockerfile --build-arg API_EXTRAS="[semantic]" -t sf-api:semantic .
#   PYTHON=python infra/scripts/measure_api_memory.sh sf-api:base false
#   PYTHON=python infra/scripts/measure_api_memory.sh sf-api:semantic true
#
# Workload: apps/api/scripts/assistant_smoke.py (demo corpus ingestion + assistant runs);
# extra smoke arguments via SMOKE_ARGS. Exits non-zero if the smoke test fails or the
# container is OOM-killed or stops; containers and network are always removed.
# Prints idle-after-boot, peak during the workload, final usage, and whether it was OOM-killed.
set -euo pipefail
IMAGE=${1:?image}; SEMANTIC=${2:-false}; LIMIT=${LIMIT:-512m}; PORT=${PORT:-8200}
PYTHON=${PYTHON:-python}
NET=sf-mem; DB=sf-mem-db; API=sf-mem-api
cleanup() { docker rm -f "$API" "$DB" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" -e POSTGRES_USER=sf -e POSTGRES_PASSWORD=sf \
  -e POSTGRES_DB=sf pgvector/pgvector:pg16 >/dev/null
until docker exec "$DB" pg_isready -U sf >/dev/null 2>&1; do sleep 1; done; sleep 2
secret() { "$PYTHON" -c "import secrets; print(secrets.token_urlsafe(48))"; }
fernet() { "$PYTHON" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"; }
docker run -d --name "$API" --network "$NET" --memory "$LIMIT" --memory-swap "$LIMIT" -p "$PORT:8000" \
  -e SF_ENVIRONMENT=production -e SF_JWT_SECRET="$(secret)" -e SF_CREDENTIALS_KEYS="$(fernet)" \
  -e SF_DATABASE_URL=postgresql://sf:sf@"$DB":5432/sf -e SF_EMBEDDED_WORKER=true \
  -e SF_MIGRATE_ON_START=true -e SF_DATABASE_POOL_SIZE=5 -e SF_DATABASE_MAX_OVERFLOW=5 \
  -e SF_SEMANTIC_EMBEDDER_ENABLED="$SEMANTIC" -e SF_RATE_LIMIT_ENABLED=false \
  -e SF_LOG_JSON=true "$IMAGE" >/dev/null
until curl -sf "http://127.0.0.1:$PORT/readyz" >/dev/null; do
  [ "$(docker inspect -f '{{.State.Running}}' "$API")" = true ] || { docker logs "$API" | tail -20; exit 1; }
  sleep 1
done
sleep 5
# cgroup v2 counters: memory.peak is the true high-water mark (no sampling gaps).
cg() { MSYS_NO_PATHCONV=1 docker exec "$API" cat "/sys/fs/cgroup/$1" | awk '{printf "%.1f", $1/1048576}'; }
idle=$(cg memory.current)
idle_peak=$(cg memory.peak)
set +e  # from here on, report everything; the exit status is decided at the end
# shellcheck disable=SC2086  # SMOKE_ARGS is intentionally word-split (e.g. "--model x")
"$PYTHON" apps/api/scripts/assistant_smoke.py "http://127.0.0.1:$PORT" --timeout 240 ${SMOKE_ARGS:-} > /tmp/sf-mem-smoke.log 2>&1
SMOKE=$?
sleep 3
running=$(docker inspect -f '{{.State.Running}}' "$API" 2>/dev/null)
oom=$(docker inspect -f '{{.State.OOMKilled}}' "$API" 2>/dev/null)
if [ "$running" = true ]; then
  final=$(cg memory.current); peak=$(cg memory.peak)
  # memory.current/peak include reclaimable page cache; "anon" is process memory proper.
  stat() { MSYS_NO_PATHCONV=1 docker exec "$API" awk -v k="$1" '$1==k {printf "%.1f", $2/1048576}' /sys/fs/cgroup/memory.stat; }
  anon=$(stat anon); file=$(stat file)
else final="n/a (container stopped)"; peak="n/a"; anon="n/a"; file="n/a"; fi
echo "image=$IMAGE semantic=$SEMANTIC limit=$LIMIT"
echo "after_boot_MiB=$idle boot_peak_MiB=$idle_peak overall_peak_MiB=$peak final_MiB=$final"
echo "final_anon_MiB=$anon final_page_cache_MiB=$file"
echo "oom_killed=${oom:-unknown} running=${running:-unknown} restarts=$(docker inspect -f '{{.RestartCount}}' "$API" 2>/dev/null)"
echo "smoke_exit=$SMOKE ($(tail -1 /tmp/sf-mem-smoke.log 2>/dev/null))"
grep -E "embedder|run |FAIL" /tmp/sf-mem-smoke.log | head -12
if [ "$SMOKE" != 0 ] || [ "$oom" = true ] || [ "$running" != true ]; then
  echo "--- smoke log (last 15 lines) ---"; tail -15 /tmp/sf-mem-smoke.log
  echo "--- API warnings/errors (last 10) ---"
  docker logs "$API" 2>&1 | grep -E '"level": "(warning|error|critical)"|Traceback|Error' | tail -10 | cut -c1-600
  # Most recent assistant traceback, if any (JSON log line with an "exception" field).
  docker logs "$API" 2>&1 | grep -E '"exception"' | tail -1 | "$PYTHON" -c "
import json, sys
line = sys.stdin.read().strip()
try:
    print('--- last traceback ---'); print(json.loads(line)['exception'][-1500:])
except Exception:
    pass"
fi
# Fail the measurement unless the workload passed and the container survived.
if [ "$SMOKE" != 0 ]; then exit "$SMOKE"; fi
if [ "$oom" = true ] || [ "$running" != true ]; then exit 3; fi
exit 0
