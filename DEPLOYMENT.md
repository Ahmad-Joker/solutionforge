# Deployment

> Cloud deployment (AWS) is Phase 15 and doesn't exist yet. This page covers what runs today.

## Configuration

All settings are environment variables prefixed `SF_` (see `apps/api/src/solutionforge/core/config.py`
and `.env.example`).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SF_ENVIRONMENT` | no | `dev` | `dev` / `test` / `staging` / `production` |
| `SF_DATABASE_URL` | yes (prod) | local Postgres | `postgresql+asyncpg://…` |
| `SF_JWT_SECRET` | **yes outside dev/test** | ephemeral in dev | ≥ 32 chars; the app refuses to start without it in staging/production |
| `SF_ACCESS_TOKEN_TTL_SECONDS` | no | 900 | 60–86400 |
| `SF_REFRESH_TOKEN_TTL_SECONDS` | no | 1209600 | |
| `SF_CREDENTIALS_KEYS` | **yes outside dev/test** | ephemeral in dev | comma-separated Fernet keys, the first one encrypts; prepend a new key to rotate. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SF_REDIS_URL` | recommended in prod | unset | shared rate-limit state across replicas; unset = in-memory per process |
| `SF_MAX_REQUEST_BYTES` | no | `8388608` | hard cap on request bodies (enforced while streaming) |
| `SF_OTEL_EXPORTER_OTLP_ENDPOINT` | no | unset (tracing off) | OTLP/HTTP base URL, e.g. `http://otel-collector:4318`; traces go to `<url>/v1/traces` |
| `SF_METRICS_TOKEN` | **yes to scrape outside dev/test** | unset | bearer token for `GET /metrics`; without it the endpoint returns 404 in staging/production |
| `SF_WORKER_METRICS_PORT` | no | unset | worker Prometheus port (private network only) |
| `SF_DATABASE_POOL_SIZE` / `SF_DATABASE_MAX_OVERFLOW` | no | `10` / `20` | per-process pool; lower it on free/serverless Postgres |
| `SF_MIGRATE_ON_START` | no | `false` | container runs `alembic upgrade head` before serving; for single-instance hosts without a release step |
| `PORT` | no | `8000` | listen port when the platform assigns one (Render, Cloud Run) |
| `SF_RATE_LIMIT_ENABLED` | no | `true` | |
| `SF_LLM_ENABLE_MOCK` | no | `true` | deterministic mock provider (models `mock:mock-1`, `mock:mock-fast`, synthetic prices) |
| `SF_ANTHROPIC_API_KEY` | no | unset | enables the `anthropic:*` models (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`) |
| `SF_LLM_PRICE_OVERRIDES` | no | `{}` | JSON `{"provider:model": {"input","output","cache_read","cache_write"}}` in USD per MTok |
| `SF_LLM_BREAKER_FAILURE_THRESHOLD` / `_RECOVERY_SECONDS` | no | `5` / `30` | per-model circuit breaker |
| `SF_WORKER_CONCURRENCY` / `SF_WORKER_POLL_INTERVAL_SECONDS` | no | `4` / `1.0` | worker process |
| `SF_CORS_ORIGINS` | no | `["http://localhost:3000"]` | JSON list |
| `SF_LOG_JSON` / `SF_LOG_LEVEL` | no | `true` / `INFO` | |

## Web (apps/web)

| Variable | Default | Notes |
|---|---|---|
| `SF_API_URL` | `http://localhost:8000` | server-side only (BFF → API); never exposed to the browser |

Run it with `npm run build && npm start`, which uses the standalone server
(`node .next/standalone/server.js`, after copying `.next/static`; the Docker image does this).
In production, terminate TLS in front of the web app so cookies get the `Secure` flag
(automatic when `NODE_ENV=production`). Keep the API on a private network and set uvicorn's
`FORWARDED_ALLOW_IPS` to the BFF's address only.

## Free hosting

Render (dashboard + API with the embedded worker, one blueprint) + Supabase or Neon
(Postgres + pgvector), $0/month. Vercel works for the dashboard too. Step by step, with a local rehearsal of the exact configuration:
[docs/DEPLOY_FREE.md](docs/DEPLOY_FREE.md). Database URLs in the libpq style that hosts hand
out (`postgres://…?sslmode=require&channel_binding=…`) are normalized for the async driver
automatically.

## Local stack

```bash
cp .env.example .env
docker compose up --build --wait   # postgres(pgvector), redis → migrate → api, 2× worker, web
python apps/api/scripts/smoke.py http://localhost:8000 --dev
```

The `migrate` service runs `alembic upgrade head` once and exits. `api` and the workers
start only after it succeeds. The web dashboard is on http://localhost:3000.

**Verified (2026-10-02, Docker 29.6):** the stack builds from scratch and every container
reports healthy. The smoke test passes against it: a workflow ran on one of the two worker
replicas with a tool call and a metered LLM call, and tenant isolation held. The
`observability` profile was also verified:
- Prometheus scraped the API and **both** worker replicas (DNS discovery);
- all 7 alert rules loaded healthy;
- all 19 dashboard expressions are valid PromQL against live data;
- Grafana provisioned the dashboard and both data sources;
- Jaeger showed a single trace running from the API container into a worker container
  (request → execution → steps → tool / LLM).

## Smoke test

`apps/api/scripts/smoke.py <base-url> [--metrics-token T] [--dev]` is the post-deploy check
the release pipeline runs. It needs only `httpx`. It creates a throwaway user and org, then
covers:
- probes;
- security headers;
- auth required;
- `/metrics` not public (skipped with `--dev`);
- a full workflow run through the worker, including a tool call and a metered LLM call
  (mock provider, so no spend);
- cross-tenant access denied.

Exit code 0 means healthy.

## Release pipeline

`.github/workflows/release.yml` runs on a `v*.*.*` tag or a manual trigger:

1. **Full CI** (the CI workflow, reused). Covers:
   - lint and types;
   - unit, integration and security tests on PostgreSQL with coverage gates;
   - SQLite, web and E2E jobs;
   - `pip-audit`, `npm audit` and the secret scan.
2. **Container smoke test:** builds the images, starts the compose stack in CI, runs the
   smoke test and checks the web login page. This is the "eval" gate for the platform
   itself.
3. **Publish:** pushes `api` and `web` to GHCR with provenance and SBOM attestations. Later
   stages deploy by **digest**, never by mutable tag.
4. **Staging:** `infra/deploy/deploy.sh staging <digests>`, then the smoke test against
   `STAGING_URL`.
5. **Production:** a GitHub environment with required reviewers (a human approves), then
   deploy and smoke test. **If the production smoke test fails, it automatically runs
   `deploy.sh production --rollback`.**

Stages 4–5 run only when the repository variable `DEPLOY_ENABLED` is `"true"`.
`infra/deploy/deploy.sh` documents the contract:
- migrations are expand/contract, so a rollback never needs a down-migration;
- services roll by digest;
- the script waits for health;
- `--rollback` redeploys the previous digests.

Until a cloud target exists (Phase 15), the script **refuses to run** rather than
pretending to deploy.

## Image

`infra/docker/api.Dockerfile` is a multi-stage build that runs as a non-root user and includes a
`HEALTHCHECK` on `/healthz`. The same image runs migrations (`alembic upgrade head`) and the API.

## Probes

- Liveness: `GET /healthz`. Checks nothing external, so a DB outage doesn't restart-loop the pods.
- Readiness: `GET /readyz`. `SELECT 1` against the DB with a 2 s timeout. Returns 503 when degraded.
