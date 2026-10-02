#!/bin/sh
# API container entrypoint.
#  - Listens on $PORT when the platform assigns one (Render, Cloud Run, Heroku-style), else 8000.
#  - WEB_CONCURRENCY=N runs N uvicorn worker processes (one Python process saturates one
#    core; measured in docs/PERFORMANCE.md). Each process has its own DB pool: budget
#    N × (SF_DATABASE_POOL_SIZE + SF_DATABASE_MAX_OVERFLOW) under Postgres max_connections.
#    Metrics are then aggregated across processes (prometheus multiprocess mode).
#  - SF_MIGRATE_ON_START=true runs `alembic upgrade head` first: for single-instance hosts
#    without a release step. With several replicas, run the `migrate` job instead.
#  - Forwarded headers are trusted only from FORWARDED_ALLOW_IPS (default: localhost).
set -eu
WORKERS="${WEB_CONCURRENCY:-1}"
if [ "$WORKERS" -gt 1 ] && { [ -z "${SF_JWT_SECRET:-}" ] || [ -z "${SF_CREDENTIALS_KEYS:-}" ]; }; then
  echo "api-start: WEB_CONCURRENCY=$WORKERS needs SF_JWT_SECRET and SF_CREDENTIALS_KEYS set:" >&2
  echo "  otherwise each process generates its own and rejects the others' tokens." >&2
  exit 1
fi
if [ "$WORKERS" -gt 1 ]; then
  export PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus-multiproc}"
  rm -rf "$PROMETHEUS_MULTIPROC_DIR" && mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
fi
if [ "${SF_MIGRATE_ON_START:-false}" = "true" ]; then
  alembic upgrade head
fi
exec uvicorn solutionforge.main:app_factory --factory \
  --host 0.0.0.0 --port "${PORT:-8000}" --workers "$WORKERS" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
