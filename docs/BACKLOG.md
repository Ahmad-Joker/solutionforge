# Implementation backlog

Each phase runs the loop PLAN → IMPLEMENT → TEST → REVIEW → BREAK → FIX → RETEST → DOCUMENT.
A phase is done only when all of its acceptance criteria are checked **and backed by tests
or a reproducible command**.

## Phase 0 — Architecture & repository ✅

- [x] Git repo, monorepo layout, `.gitignore`, `.env.example`, Makefile
- [x] ARCHITECTURE.md (product architecture, components, domain model, API boundaries, execution model)
- [x] SECURITY.md threat model v1
- [x] ADRs 0001–0005
- [x] CI pipeline definition (lint, types, pip-audit, tests on Postgres + SQLite, Docker build)
- [x] Dockerfile + docker compose (Postgres/pgvector, Redis, migrate, API)

## Phase 1 — FastAPI + PostgreSQL + orgs/users/auth ✅

Acceptance criteria:

- [x] A user can register (email normalized, Argon2id hash, 12–128 char password).
- [x] A user can log in and receives a short-lived JWT access token and a rotating refresh token.
- [x] Refresh rotates; reusing a rotated token revokes the whole session family; logout revokes it.
- [x] A user can create an organization and becomes its OWNER.
- [x] A user can join an organization via an email-bound, single-use, expiring invitation.
- [x] Roles OWNER/ADMIN/OPERATOR/VIEWER with a deterministic permission matrix.
- [x] Role changes obey rank rules; the last owner cannot be demoted or removed.
- [x] Every tenant route resolves membership first; foreign orgs are indistinguishable from missing ones.
- [x] Security-relevant events are written to an append-only audit log, with secrets redacted.
- [x] Schema is created by Alembic migrations; a test fails on model/migration drift.
- [x] Health (`/healthz`) and readiness (`/readyz`, DB check) probes.
- [x] Uniform error envelope with request IDs; no input echo, no stack traces.
- [x] Tests: unit + integration + security (tenant isolation, RBAC matrix, token attacks, races).
- [ ] Suite executed against PostgreSQL. **Configured in CI, not yet run** (no Docker/Postgres on the dev machine).

## Phase 2 — Workflow execution engine ✅

- [x] Workflow / immutable WorkflowVersion / append-only WorkflowDeployment / Execution / ExecutionStep (migration 0002)
- [x] Definitions compiled when a version is created: graph integrity, reachability, configs, references
- [x] Safe expression language (references, templates, predicates; no eval, no regex)
- [x] Step registry with `transform`, `condition`, `approval`, `fail`; extensible for llm/tool/retrieve
- [x] Explicit deployments; rollback = deploy an older version; unreleased versions need `workflow:write`
- [x] Worker with bounded concurrency, jittered polling and graceful drain (`python -m solutionforge.worker`)
- [x] Leases and fenced checkpoints: a crash mid-step resumes on another worker, and completed steps don't re-run (tested)
- [x] Poison-pill guard: repeated worker crashes use up the step's attempts (tested)
- [x] Retries with exponential backoff (scheduled via `run_after`, no busy-waiting); per-step timeouts; `on_error` fallback
- [x] Budgets: `max_steps` (an infinite loop ends as `budget_exceeded`, tested) and `max_active_seconds`
- [x] Suspend/resume (approval); cancel (queued/waiting stop immediately, running stops before the next step)
- [x] Idempotent execution creation (per tenant, race-safe)
- [x] Output and state size caps; handler exceptions contained and not leaked; handler scope isolation
- [x] Tenant isolation and RBAC matrix tests extended to every workflow/execution route
- [x] Live run with separate API and worker processes
- [ ] Suite run on PostgreSQL (CI only; no local Docker/Postgres)

## Phase 3 — LLM provider abstraction
`LLMProvider` protocol; Anthropic + OpenAI-compatible + deterministic `MockProvider`.
Structured output with schema validation and repair-retry; fallback chain; circuit breaker;
UsageRecord for every call (tokens, latency, cost); org budgets (daily/monthly/per-execution).
**Accept when:** invalid JSON from the model is retried and then fails cleanly; exceeding the
budget stops execution; no module outside `llm/` imports a vendor SDK (import-linter).

## Phase 4 — Tool interface + simulated tools
`Tool` spec (name, description, input/output schema, permission, risk level, timeout, retry,
idempotency). Simulated CRM, ticketing, email outbox, order DB, calendar. These are real
Postgres-backed services with seeded data, not mocks. Encrypted connector credentials.

## Phase 5 — Agent orchestration
Bounded tool-use loop step; tool selection recorded; structured decision trace (no raw chain of thought).

## Phase 6 — RAG
Ingestion (upload → parse → chunk → embed) as background jobs; pgvector HNSW; metadata filters;
citations bound to chunk IDs, with a verifier that rejects unknown IDs. Later: BM25
(`tsvector`), hybrid RRF, reranking, and a retrieval eval (recall@k, MRR).

## Phase 7 — RBAC + tool permissions + policy engine
PolicyEngine (risk × tenant policy × role) → ALLOW / REQUIRE_APPROVAL / DENY. Redis login rate
limiting. Postgres RLS as defense in depth.

## Phase 8 — Human approvals
Approval requests with proposed action, parameters, reason, risk, and source execution.
Approve / Reject / Modify (the modified arguments are re-validated and re-policy-checked).
Resume across restarts.

## Phase 9 — Frontend (Next.js + TS + Tailwind)
Auth via a BFF with httpOnly cookies; orgs and members; workflow editor (JSON + form);
execution timeline; approvals inbox; eval comparison view.

## Phase 10 — Testing hardening
Playwright E2E; property tests for the policy engine; coverage gates on `security/` and `services/`.

## Phase 11 — Evaluation framework
Datasets and cases (expected output, expected and forbidden tools, expected citations);
scorers (schema validity, tool accuracy, citation accuracy, groundedness via judge + rules,
latency, cost, steps); v1 vs v2 vs v3 comparison; **deployment gate** that stores the decision
and blocks regressions.

## Phase 12 — Observability
OpenTelemetry traces (HTTP → workflow → step → model/tool), Prometheus metrics (RED, token/cost,
queue depth), Grafana dashboards as code.

## Phase 13 — Docker (full stack incl. worker, web, observability)
## Phase 14 — GitHub Actions: release pipeline (build → test → eval → staging → smoke → prod, rollback)
## Phase 15 — Cloud deployment (AWS: ECS Fargate + RDS + ElastiCache; Terraform stretch goal)
## Phase 16 — Security testing (prompt injection, malicious docs, PII extraction, tool-arg injection, looping)
## Phase 17 — Performance / load testing (k6 or Locust; measured p50/p95/p99)
## Phase 18 — Three customer case studies (Support Ops, Business Research, Ops Automation)
## Phase 19 — Documentation & portfolio polish

## Tech-debt / follow-ups discovered so far

- Audit events created in the same transaction can share a timestamp, which makes ordering
  within one request ambiguous. Add a monotonic `seq` (bigserial). This also enables a hash
  chain for tamper evidence.
- Add an import-linter contract to enforce `api → services → domain/db/security`.
- Email verification for registration (closes the enumeration gap and proves email ownership
  for invitations).
- The "current deployment" lookup and audit ordering rely on `created_at`. Add a monotonic
  sequence so ordering stays strict when two writes land in the same microsecond.
- The expression language has no arithmetic. Add a small safe set (`add`, `len`, …) when a
  real workflow needs counters.
- Worker wake-up latency equals the poll interval. Add Redis or `LISTEN/NOTIFY` wake-ups.
- Refresh-token reuse detection also fires on a benign client retry after a lost response.
  Consider a short grace window.
