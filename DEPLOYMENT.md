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

## Local stack

```bash
cp .env.example .env
docker compose up --build      # postgres(pgvector) → migrate → api ; redis
curl localhost:8000/readyz
```

The `migrate` service runs `alembic upgrade head` once and exits. `api` starts only after it succeeds.

## Image

`infra/docker/api.Dockerfile` is a multi-stage build that runs as a non-root user and includes a
`HEALTHCHECK` on `/healthz`. The same image runs migrations (`alembic upgrade head`) and the API.

## Probes

- Liveness: `GET /healthz`. Checks nothing external, so a DB outage doesn't restart-loop the pods.
- Readiness: `GET /readyz`. `SELECT 1` against the DB with a 2 s timeout. Returns 503 when degraded.
