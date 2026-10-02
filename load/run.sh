#!/usr/bin/env bash
# Run a k6 profile against a running stack and save the JSON summary.
#   load/run.sh steady|stress [docker-network] [base-url]
# Default target: the compose stack's API over its private network.
set -euo pipefail
PROFILE=${1:-steady}
NET=${2:-solutionforge_default}
BASE=${3:-http://api:8000}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/results/$PROFILE-$(date -u +%Y%m%dT%H%M%SZ).json"
mkdir -p "$HERE/results"
{ printf 'const CORPUS = '; cat "$HERE/../apps/api/tests/fixtures/support_kb.json"; printf ';\n'
  cat "$HERE/k6/mix.js"; } |
  docker run --rm -i --network "$NET" -e PROFILE="$PROFILE" -e BASE_URL="$BASE" -e RATE_SCALE="${RATE_SCALE:-1}" -e DURATION="${DURATION:-3m}" \
    grafana/k6:1.3.0 run --quiet - > "$OUT"
echo "$OUT"
