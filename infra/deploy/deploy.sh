#!/usr/bin/env bash
# Deploy hook called by .github/workflows/release.yml.
#
#   deploy.sh <staging|production> <api-digest> <web-digest>
#   deploy.sh <staging|production> --rollback
#
# Contract for the implementation (Phase 15, cloud target):
#   1. Run database migrations with the new API image (`alembic upgrade head`) as a one-off
#      task. Migrations must be backward compatible with the previous release (expand/contract),
#      so a rollback never needs a down-migration.
#   2. Roll the api, worker and web services to the given digests (never mutable tags).
#   3. Wait for health checks; exit non-zero if the rollout does not stabilise.
#   4. --rollback redeploys the digests that were running before the last deploy.
#
# Until a target exists this refuses loudly rather than pretending to deploy.
set -euo pipefail
echo "deploy.sh: no deployment target is configured yet (Phase 15)." >&2
echo "Set DEPLOY_ENABLED only after implementing this script for your cloud." >&2
exit 1
