#!/usr/bin/env bash
# Measure the API container's memory under Render-free-like limits (512 MB, embedded worker,
# migrations on start, production settings), with and without the semantic embedder.
#
#   docker build -f infra/docker/api.Dockerfile -t sf-api:base .
#   docker build -f infra/docker/api.Dockerfile --build-arg API_EXTRAS="[semantic]" -t sf-api:semantic .
#   PYTHON=python infra/scripts/measure_api_memory.sh sf-api:base false
#   PYTHON=python infra/scripts/measure_api_memory.sh sf-api:semantic true
#
# Workload: apps/api/scripts/assistant_smoke.py (demo corpus ingestion + 7 assistant runs).
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
set +e
"$PYTHON" apps/api/scripts/assistant_smoke.py "http://127.0.0.1:$PORT" --timeout 240 > /tmp/sf-mem-smoke.log 2>&1
SMOKE=$?
set -e
sleep 3
final=$(cg memory.current); peak=$(cg memory.peak)
echo "image=$IMAGE semantic=$SEMANTIC limit=$LIMIT"
echo "after_boot_MiB=$idle boot_peak_MiB=$idle_peak overall_peak_MiB=$peak final_MiB=$final"
echo "oom_killed=$(docker inspect -f '{{.State.OOMKilled}}' "$API") restarts=$(docker inspect -f '{{.RestartCount}}' "$API")"
echo "smoke_exit=$SMOKE ($(tail -1 /tmp/sf-mem-smoke.log))"
grep -E "embedder|run " /tmp/sf-mem-smoke.log | head -9
if [ "$SMOKE" != 0 ]; then
  echo "--- API errors ---"
  docker logs "$API" 2>&1 | grep -E "assistant_run_failed" | tail -1 | "$PYTHON" -c "import json,sys; print(json.loads(sys.stdin.read())[\"exception\"][-1500:])"
fi
