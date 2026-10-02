#!/bin/sh
# API container entrypoint.
#  - Listens on $PORT when the platform assigns one (Render, Cloud Run, Heroku-style), else 8000.
#  - SF_MIGRATE_ON_START=true runs `alembic upgrade head` first: for single-instance hosts
#    without a release step. With several replicas, run the `migrate` job instead.
#  - Forwarded headers are trusted only from FORWARDED_ALLOW_IPS (default: localhost).
set -eu
if [ "${SF_MIGRATE_ON_START:-false}" = "true" ]; then
  alembic upgrade head
fi
exec uvicorn solutionforge.main:app_factory --factory \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
