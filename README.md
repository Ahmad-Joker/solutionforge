# SolutionForge

**A multi-tenant platform for deploying AI workflows that are permission-controlled, human-approved where it matters, evaluated before release, and observable in production.**

> **Project status: Phase 1 of 19 complete.** This README only describes what exists and is
> tested. Planned capabilities are listed under [Roadmap](#roadmap) and marked as such in
> [ARCHITECTURE.md](ARCHITECTURE.md). Live demo, demo video and benchmark numbers will be added
> once they exist and are measured.

## What's built

| Capability | Details |
|---|---|
| **Authentication** | Argon2id passwords; 15-min JWT access tokens (pinned alg, aud/iss/typ checks); opaque rotating refresh tokens with **reuse detection that revokes the whole session family** |
| **Multi-tenancy** | Organizations and memberships; every tenant route resolves membership first; foreign resources are **indistinguishable from missing ones (404)** |
| **RBAC** | OWNER / ADMIN / OPERATOR / VIEWER → deterministic permission matrix; rank rules (no granting above your own role, no demoting a higher role); last-owner protection with row locks |
| **Invitations** | Email-bound, single-use, expiring, hashed at rest |
| **Audit log** | Security events written in the *same transaction* as the action; recursive secret redaction; **PostgreSQL trigger makes the table append-only** |
| **API quality** | Versioned `/api/v1`, OpenAPI at `/docs`, uniform error envelope, request-ID propagation, structured JSON logs, liveness and readiness probes |
| **Engineering** | Alembic migrations with a drift test; strict mypy; ruff (incl. bandit rules); CI on real PostgreSQL; Dockerfile (non-root) + compose stack |

## Architecture

```mermaid
flowchart LR
  C[Client] --> MW[Request ID / logging] --> A[Auth] --> T[Tenant resolution] --> R[/api/v1 routers/]
  R --> S[Services: permission checks + audit]
  S --> DB[(PostgreSQL + pgvector)]
  S -. planned .-> WF[Workflow engine] -. planned .-> P[Policy engine] -. planned .-> H[Human approval]
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full component diagram, domain model, API
boundaries and the durable workflow execution model. Key decisions are recorded as
[ADRs](docs/adr/).

## Quick start

**With Docker** (PostgreSQL + Redis + migrations + API):

```bash
cp .env.example .env
docker compose up --build
```

Then open http://localhost:8000/docs.

**Without Docker** (Python 3.12+; tests use SQLite automatically):

```bash
cd apps/api
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

A walkthrough of the API flow (register → login → create org → invite → audit) is in [API.md](API.md).

## Testing

```bash
cd apps/api
pytest                      # full suite (SQLite)
pytest -m security          # security regression suite only
SF_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/sf_test pytest   # PostgreSQL
```

| Suite | What it proves |
|---|---|
| `tests/unit` | RBAC matrix is monotonic; token forgery (alg=none, wrong key/aud/iss/typ) fails; redaction; config refuses weak secrets |
| `tests/integration` | Auth lifecycle, refresh rotation and reuse detection, invitations, role rules, audit trail, migration drift, **concurrent refresh / registration races resolve to exactly one winner** |
| `tests/security` | Every tenant route attacked cross-tenant (incl. confused deputy); full role × endpoint matrix over HTTP; append-only audit on PostgreSQL |

The schema is always created by running the real Alembic migrations, never `create_all`.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs ruff, mypy (strict), `pip-audit`,
the full suite on PostgreSQL (pgvector image) with coverage, a check that **security tests were
not skipped**, a migration downgrade/upgrade round trip, the suite on SQLite, and a Docker build.

## Tech stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic · PostgreSQL 16 + pgvector ·
Redis · Argon2 · PyJWT · structlog · pytest · ruff · mypy · Docker · GitHub Actions.
Planned: Next.js/TypeScript, OpenTelemetry, Prometheus, Grafana, AWS.

## Roadmap

Workflow engine → LLM provider abstraction and metering → tools/connectors → agent
orchestration → RAG with verified citations → policy engine → human approvals → dashboard →
evaluation and quality-gated deployment → observability → cloud deployment → security and load
testing → three customer case studies. Details and acceptance criteria are in
[docs/BACKLOG.md](docs/BACKLOG.md).

## Docs

[ARCHITECTURE](ARCHITECTURE.md) · [SECURITY](SECURITY.md) (threat model) · [API](API.md) ·
[DEPLOYMENT](DEPLOYMENT.md) · [CONTRIBUTING](CONTRIBUTING.md) · [ADRs](docs/adr/) · [Backlog](docs/BACKLOG.md)
