# Testing strategy

| Layer | Where | What it proves |
|---|---|---|
| Unit | `apps/api/tests/unit` | Pure logic: RBAC matrix, tokens, expressions, definition compiler, transitions, pricing, policy, chunking, BM25/RRF, citation verifier, rate limiter |
| Property-based | `tests/unit/test_properties.py` (Hypothesis, 300 cases each) | Invariants over generated inputs: expressions only fail with `ExpressionError`; secrets never survive redaction at any depth; policy is monotonic in role and never auto-runs external/high-risk tools; chunking loses no text, keeps exact offsets, never crosses headings |
| Integration | `tests/integration` | The API + engine + DB together: auth lifecycle, workflows, crash recovery, lease fencing, LLM metering and budgets, tools and idempotency, agents, RAG, approvals |
| Security regression | `tests/security` (`-m security`) | Cross-tenant attacks on every route, RBAC matrices over HTTP, token forgery, brute-force limits, prompt-injection containment, approval bypass attempts |
| Adapter | `tests/unit/test_claude_adapter.py` | The Anthropic adapter through the **real SDK** over an HTTP mock transport |
| End-to-end | `apps/web/e2e` (Playwright, Chromium) | Real browser → BFF → API with the worker embedded: cookie security, CSRF, create/version/deploy/run a workflow, approve an external action in the inbox, then audit |

## Running

```bash
cd apps/api
pytest -n auto                          # SQLite, parallel (~1.5–3 min)
SF_TEST_DATABASE_URL=postgresql+asyncpg://… pytest   # PostgreSQL, serial (~40 min locally)
pytest -m security                      # security regression suite
pytest --cov --cov-report=json && python scripts/check_coverage.py coverage.json

cd ../web
npm run build                           # postbuild copies static assets into standalone
SF_PYTHON=python npx playwright test    # starts a throwaway API + web server
```

## Gates (CI)

- Overall line+branch coverage ≥ **90%** (currently 94%).
- Per-module ≥ **90%** for security-critical code: `security/`, tenancy, authz, auth, org
  service, approvals, evaluation and the deploy gate, tool policy, executor, engine,
  transitions (`scripts/check_coverage.py`).
- `pytest -m security` must not skip anything on PostgreSQL.
- Lint (ruff), types (mypy strict, `tsc --noEmit`), `pip-audit`, a migration round trip.

A throwaway PostgreSQL for local runs:

```bash
docker run -d --name sf-pgtest -e POSTGRES_USER=sf -e POSTGRES_PASSWORD=sf -e POSTGRES_DB=sf   -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
```

**Why both backends matter.** The first PostgreSQL run (2026-10-02) found a
production-only defect that the SQLite runs had hidden for several phases: keyword search
ANDed every term. See [RETRIEVAL.md](RETRIEVAL.md). Treat the SQLite run as a fast inner
loop, not as evidence about production behaviour.

## Conventions that keep tests trustworthy

- Fault injection instead of mocks for failure paths (instrumented steps and tools, a
  scripted mock LLM). Simulated systems are real tables.
- Tests use a fast Argon2 profile (`fast-insecure-test`). The settings refuse it outside
  `SF_ENVIRONMENT=test`.
- Assertions about leaked content use markers that can't occur in hex IDs. A `"4111"`
  marker once collided with random UUIDs about 2% of the time; that flaky test was fixed at
  its root, not retried.
