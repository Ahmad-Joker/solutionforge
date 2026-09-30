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
| `SF_CORS_ORIGINS` | no | `["http://localhost:3000"]` | JSON list |
| `SF_LOG_JSON` / `SF_LOG_LEVEL` | no | `true` / `INFO` | |

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
