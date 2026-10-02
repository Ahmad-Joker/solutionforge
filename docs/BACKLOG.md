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
- [x] Suite executed against PostgreSQL 16 + pgvector (2026-10-02, locally via Docker; also in CI), including the append-only audit trigger test.

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

## Phase 3 — LLM provider abstraction ✅

- [x] `LLMProvider` protocol; deterministic scriptable `MockProvider`; Anthropic adapter (official SDK, SDK retries disabled)
- [x] Adapter tested through the real SDK with an HTTP mock transport (request shape, usage/cache mapping, error classes, no hidden retries)
- [x] Structured output: native schema for capable providers, prompt instruction otherwise; JSON-Schema validation; repair turns
- [x] Retries with jittered exponential backoff (honours `retry-after`); per-call timeout; model fallback chain; per-model circuit breaker
- [x] Every attempt metered (`usage_records`, integer micro-USD); price table; unpriced models rejected
- [x] Budgets: org daily/monthly/per-execution (API + audit), workflow `max_cost_usd` / `max_llm_tokens`; pre-checked with worst-case estimates
- [x] `llm` step type: templated prompts, schema-validated JSON usable by later steps, failure → retryable/non-retryable/budget_exceeded
- [x] Usage APIs (summary, records) and per-execution LLM usage in the execution detail
- [x] Vendor SDK import boundary enforced by a test
- [x] Tenant isolation, RBAC, key secrecy and data minimisation tests
- [x] Live run: API + worker + mock provider; cost verified by hand
- [ ] Real Anthropic call (needs an API key; the adapter is tested against recorded API shapes only)
- [ ] OpenAI-compatible adapter (deferred; the abstraction is proven with two providers)

## Phase 4 — Tool interface + simulated tools ✅

- [x] `ToolSpec` (typed I/O, risk level, permission, timeout, retries, idempotency, credentials, config keys); MCP `tools/list` descriptors
- [x] ToolExecutor: catalog → installation → validation → policy gate → replay ledger → timeout/retry → output validation → record + audit
- [x] Simulated CRM, orders, ticketing, email (draft/send), payments (refund); deterministic demo seed
- [x] Per-tenant installations; Fernet-encrypted, rotatable, write-only credentials (audited by field name)
- [x] `tool` step type with compile-time tool/argument validation
- [x] Per-visit idempotency keys (`visit_seq`): crash after a side effect doesn't duplicate; loops get fresh keys (tested)
- [x] External/high-risk tools blocked pending the approval flow (P8); tenant can require approval for low-risk writes
- [x] Tenant isolation, RBAC and input-hardening tests
- [ ] Calendar connector (deferred; not needed by the three scenarios)
- [ ] Serving the catalog as an actual MCP server, or consuming external MCP servers (descriptors are ready)

Original plan:
`Tool` spec (name, description, input/output schema, permission, risk level, timeout, retry,
idempotency). Simulated CRM, ticketing, email outbox, order DB, calendar. These are real
Postgres-backed services with seeded data, not mocks. Encrypted connector credentials.

## Phase 5 — Agent orchestration ✅

- [x] `agent` step: schema-constrained action protocol, allowlisted tools, policy-gated execution
- [x] Caps: turns, tool calls, identical-call repeats; LLM budgets on every turn; invalid actions fail closed
- [x] Errors and policy refusals fed back as untrusted `<observation>` data; final answer validated against a schema
- [x] Decision trace (action, tool, sanitized args, outcome, short stated reason), with no hidden reasoning
- [x] Per-action idempotency keys (turn + args hash); key reuse with different args rejected
- [x] Injection test: model scripted to obey an injected instruction; refund and send are still blocked
- [ ] Native tool-use adapter (optional; parallel tool calls)

Original plan:
Bounded tool-use loop step; tool selection recorded; structured decision trace (no raw chain of thought).

## Phase 6 — RAG ✅

- [x] Knowledge bases and documents per tenant; idempotent upload (content hash); durable ingestion jobs with dead letter and retry API
- [x] Structure-aware chunking with exact offsets and heading-bounded chunks (coverage and offset invariants tested)
- [x] pgvector `vector(1024)` + HNSW; Postgres FTS + GIN; hybrid RRF; metadata filters in SQL
- [x] Dense relevance floor calibrated by measurement (docs/RETRIEVAL.md)
- [x] `retrieve` and `grounded_answer` steps; citations restricted by schema and verified in code
- [x] Measured recall@1/3/5 and MRR for dense, keyword and hybrid on a labelled fixture
- [x] Tenant isolation (incl. same-name KBs across orgs) and RBAC tests
- [ ] Semantic embedding provider (Voyage/OpenAI/local) + re-measure; cross-encoder reranking
- [ ] File upload (PDF/DOCX parsing); today the API accepts text/Markdown content
- [ ] Embedding cost metering (needed once a paid embedder exists)

Original plan:
Ingestion (upload → parse → chunk → embed) as background jobs; pgvector HNSW; metadata filters;
citations bound to chunk IDs, with a verifier that rejects unknown IDs. Later: BM25
(`tsvector`), hybrid RRF, reranking, and a retrieval eval (recall@k, MRR).

## Phase 7 — RBAC + tool permissions + policy engine ✅

- [x] Tool policy evaluated as the execution's initiator (current role, resolved per step); removed or demoted initiators denied (tested)
- [x] Org tool policy (`blocked_tools`, `blocked_risk_levels`, `auto_allow_up_to`) via an audited API
- [x] Requester permission separated from approver permission
- [x] Rate limiting: login (account + IP), registration, refresh; Redis or in-memory; fail-open on backend errors
- [ ] Postgres RLS (deferred with rationale, ADR-0011; needs real PostgreSQL)

Original plan:
PolicyEngine (risk × tenant policy × role) → ALLOW / REQUIRE_APPROVAL / DENY. Redis login rate
limiting. Postgres RLS as defense in depth.

## Phase 8 — Human approvals ✅

- [x] Durable approval requests (proposed args, reason, risk, permission, expiry) per step visit
- [x] Approve / reject / approve-with-modified-args (re-validated); `on_reject` routing
- [x] Approver permission from policy; four-eyes for high risk; final decisions; audit trail linking actions to approvals
- [x] Resume re-runs the same step visit (`RERUN`, same idempotency key); policy re-checked; exactly-once execution
- [x] Expiry sweeper in the worker; cancellation cascades; the resume endpoint can't bypass
- [ ] Notifications (email/Slack) for new approval requests
- [ ] Approvals from inside agent loops (by design, agents propose and tool steps request)

Original plan:
Approval requests with proposed action, parameters, reason, risk, and source execution.
Approve / Reject / Modify (the modified arguments are re-validated and re-policy-checked).
Resume across restarts.

## Phase 9 — Frontend (Next.js + TS + Tailwind) ✅

- [x] BFF auth with httpOnly cookies, refresh rotation in the proxy, CSRF header, path validation, security headers
- [x] Pages: orgs, overview, workflows/versions/deploy/run, execution timeline + agent trace + citations, approvals inbox, knowledge + search, tools + policy, usage + budget, audit
- [x] Strict TypeScript, production standalone build, Dockerfile, compose service, CI job
- [ ] Visual design polish; a graphical workflow editor (JSON editor today)

Original plan:
Auth via a BFF with httpOnly cookies; orgs and members; workflow editor (JSON + form);
execution timeline; approvals inbox; eval comparison view.

## Phase 10 — Testing hardening ✅

- [x] Parallel suite (pytest-xdist); fast Argon2 profile allowed only in `SF_ENVIRONMENT=test`
- [x] Hypothesis property tests (expressions, policy, chunking)
- [x] Coverage gates: ≥90% overall and ≥90% per critical module (`scripts/check_coverage.py`)
- [x] Playwright E2E through the real BFF and API with an embedded worker (CI job)
- [x] PostgreSQL-only paths executed locally (2026-10-02)

## Phase 11 — Evaluation framework ✅

- [x] Datasets, cases (expectations validated, unknown keys rejected), runs, per-case results (migration 0008)
- [x] Runs execute pinned versions through the real engine; invalid case inputs fail without running
- [x] Deterministic scorers: status, output subset, JSON Schema, expected/forbidden tools, citations verified against retrieved chunks, step/cost limits, lexical groundedness
- [x] Aggregates: pass rate, tool selection, schema validity, citation accuracy, groundedness (lexical), p50/p95 latency (nearest rank), cost/case, steps, error rate, security cases; unmeasured → null
- [x] Background finalizer with deadline; waiting/queued executions and their approvals are cancelled after scoring
- [x] Multi-run comparison with a per-case pass matrix
- [x] Per-workflow gate policy enforced inside `deploy()`; every attempt stored as a `DeploymentDecision`; 409 on block; OWNER-only audited override
- [x] Dashboard evaluation page (datasets, cases, runs, comparison, gate, decisions) + E2E
- [ ] Dataset versioning/snapshots so baseline and candidate are scored on the same case set
- [ ] "N consecutive passing runs" option against cherry-picking non-deterministic runs
- [ ] Optional judge-based semantic groundedness as a separately labelled metric

Original plan:
Datasets and cases (expected output, expected and forbidden tools, expected citations);
scorers (schema validity, tool accuracy, citation accuracy, groundedness via judge + rules,
latency, cost, steps); v1 vs v2 vs v3 comparison; **deployment gate** that stores the decision
and blocks regressions.

## Phase 12 — Observability ✅

- [x] OTel tracing: FastAPI server spans → `workflow.execution` (continued from the persisted `traceparent`, migration 0009) → `workflow.step` → `tool.invoke` / `llm.generate`
- [x] Span content policy (IDs/metadata only) enforced by test; trace IDs in log lines
- [x] Prometheus metrics: HTTP RED by route template, executions, steps, LLM calls/tokens/cost/latency, tools by outcome, approvals, ingestion backlog, eval runs, gate decisions
- [x] No tenant/user labels and bounded cardinality (tested by scraping after real traffic)
- [x] `/metrics` bearer-token protected (hidden outside dev/test without a token); worker metrics port
- [x] Grafana dashboard generated from code + drift test; Prometheus alert rules; compose `observability` profile (Prometheus, Grafana, Jaeger)
- [ ] Compose profile executed end to end (needs Docker)
- [ ] Log shipping (Loki/CloudWatch), exemplars, SLOs from measured traffic

Original plan:
OpenTelemetry traces (HTTP → workflow → step → model/tool), Prometheus metrics (RED, token/cost,
queue depth), Grafana dashboards as code.

## Phase 13 — Docker (full stack incl. worker, web, observability) ✅

- [x] Compose stack: postgres(pgvector), redis, migrate, api, 2× worker, web; observability profile (Prometheus, Grafana, Jaeger)
- [x] Verified locally from scratch: all containers healthy; smoke test passes (worker replica ran tool + LLM steps); Prometheus scrapes api + both workers, 7 alert rules healthy, all dashboard queries valid; Grafana provisioned; one Jaeger trace spans api → worker
- [x] Full test suite executed against PostgreSQL 16 + pgvector (previously CI-only) — found and fixed a production-only retrieval bug (see RETRIEVAL.md)
- [ ] Image vulnerability scanning (Trivy) and image size budget
## Phase 14 — Release pipeline ✅ (cloud stages gated on Phase 15)

- [x] `release.yml`: reused full CI → container smoke test → GHCR publish (digests, provenance, SBOM) → staging → smoke → production (required reviewers) → smoke → automatic rollback
- [x] `scripts/smoke.py` post-deploy check (verified against a local server and the compose stack)
- [x] `infra/deploy/deploy.sh` contract (expand/contract migrations, digest rollouts, rollback); refuses to run until a target exists
- [ ] Pipeline executed on GitHub (needs the repository pushed to GitHub)
- [ ] Deploy implementation + staging/production environments (Phase 15)
## Phase 15 — Cloud deployment (AWS: ECS Fargate + RDS + ElastiCache; Terraform stretch goal)
## Phase 16 — Security testing ✅

- [x] Red-team suite (models scripted to comply): indirect injection via KB documents, fake citations, tool-arg injection, malicious documents, PII in logs, runaway cost loops
- [x] Fixed: streaming body-size limit (413); NUL rejected at the edge, rejected in tool args, scrubbed in JSON writes; single-line fields reject all Unicode line breaks + bidi overrides; ASCII-only reference digits; API security headers; Trojan-Source guard test; pip upgraded
- [x] `redact` step: deterministic PII masking (email, Luhn cards, mod-97 IBANs, US SSNs, phones) with property tests
- [x] CI: `npm audit` (prod, high+), TruffleHog verified-secret scan; `pip-audit` clean
- [ ] Strict numeric mode for tool contracts; DAST (ZAP) against a deployed env; image scanning (Trivy); dependency-update bot
- [ ] NER-based PII detection (names/addresses) as a separate, labelled control

Findings and residual risks: [SECURITY_TESTING.md](SECURITY_TESTING.md).
## Phase 17 — Performance / load testing ✅ (local laptop measurements)

- [x] k6 mix (reads, workflow starts, hybrid search), steady + stress profiles, rate scaling; per-endpoint p50/p95/p99, repeated runs with ranges
- [x] Measured: SLO held at ~68–100 req/s on a 4-core laptop; saturates ~130 req/s; graceful 503 shedding under 3× overload; end-to-end workflow latency
- [x] Fixed: connection-pool deadlock in KB search (regression test with a 1-connection pool); pool exhaustion → 503 + Retry-After; multi-process API with Prometheus multiprocess metrics; shared dev secrets (also fixed workers unable to decrypt API-stored credentials)
- [ ] Measure on a dedicated server; real-LLM latency; larger corpora
- [ ] Faster load shedding (concurrency limit) instead of waiting out the pool timeout; `LISTEN/NOTIFY` worker wake-ups
## Phase 18 — Customer case studies ✅

- [x] Support ops (late-order resolution), research (policy assistant), ops automation (VIP delay sweep): workflow + evaluation dataset + write-up in `cases/`
- [x] Measured on SQLite and PostgreSQL; runner CLI (`scripts/case_study.py`) for any deployment; CI runs all three
- [x] Evaluation fixes found while building them: approval-pending tool calls now count as *attempted* (expected/forbidden tool checks were blind to them); an input rejection now satisfies a case that expects `failed`
- [ ] Re-measure with a real model (answer/brief quality, citation accuracy)
- [ ] Built-in scheduler for recurring workflows (the ops sweep is API-triggered)
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
- Budget pre-checks can overshoot under concurrency (bounded by concurrency × worst case);
  consider spend reservations for strict caps.
- The circuit breaker is per process; share its state via Redis when running many workers.
- Verify the Anthropic price table against the pricing page before billing customers on it.
- Streaming for large `max_tokens` (the SDK recommends streaming for long outputs).
- Tool authorization ignores *who* triggered the execution. Phase 7 adds the role check
  (`required_permission`) for the execution's initiator.
- Same-key concurrent tool calls can both reach the connector. Write connectors must enforce
  the key; consider a lease on `tool_calls` rows instead.
- Refresh-token reuse detection also fires on a benign client retry after a lost response.
  Consider a short grace window.
