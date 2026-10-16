# Capability audit: QovaTech AI Intern requirements

Audited on 2026-10-16 on branch `feat/assistant-tool-calling`, starting from `c09a5ce`.
Earlier claims (test counts, retrieval scores) were treated as unverified and re-checked.

**Statuses**

| Status | Meaning |
|---|---|
| **IMPLEMENTED AND VERIFIED** | Code exists, and a command run on this date exercised it |
| **PARTIAL** | Exists with a stated gap |
| **MISSING** | Not built |
| **BLOCKED** | Built, but cannot be verified without something unavailable (an API key) |

**Baseline before changes:** `pytest -n 6` → 679 passed, 2 skipped (SQLite). Ruff and mypy
(strict) clean.

**After changes:** `pytest -n 6` → **734 passed, 2 skipped**. Ruff, ruff format and mypy
(strict, 121 files) clean. `npx playwright test` → **6 passed**, including the new
assistant E2E.

## Before → after

| Capability | Before (evidence) | After | Evidence after |
|---|---|---|---|
| Shipped LLM app, Python + TS | IMPLEMENTED: FastAPI + Next.js platform, live on Render | IMPLEMENTED AND VERIFIED, plus a new assistant page | `apps/web/app/(app)/o/[org]/assistant/page.tsx`; `e2e/assistant.spec.ts` passes |
| Provider SDK use | PARTIAL: `AnthropicProvider.complete()` only, structured output, no tools | IMPLEMENTED AND VERIFIED through the real SDK over a mock HTTP transport. Live calls **BLOCKED** (no key) | `llm/providers/claude.py::converse`; `tests/unit/test_tool_calling_adapter.py`; `test_full_loop_through_the_real_sdk` |
| Function calling / tool use | MISSING: the agent used a JSON-in-text action protocol (`agents/runner.py`) | IMPLEMENTED AND VERIFIED: native `tools` / `tool_use` / `tool_result`, three allowlisted tools, Pydantic validation, `is_error` results | `assistant/tools.py`, `assistant/service.py`; 26 integration tests in `test_assistant.py` |
| Bounded agent loop | PARTIAL: the workflow engine had step limits, the agent loop had no tool-level limits | IMPLEMENTED AND VERIFIED: limits on turns, tool calls, deadline and run budget; bounded transient retries; cancellation; named terminal states | `test_turn_limit`, `test_tool_call_limit`, `test_deadline_bounds_a_hanging_provider`, `test_run_budget`, `test_transient_errors_*`, `test_retries_are_bounded`, `test_cancellation_between_steps` |
| Authorization and tenancy | IMPLEMENTED (RBAC, `scoped_select`, audit) | IMPLEMENTED AND VERIFIED for the assistant: tool permissions use the user's role; lookups and drafts are tenant-scoped | `test_viewer_cannot_run_or_approve`, `test_tool_permissions_follow_the_user_not_the_model`, `test_tenant_boundary_on_tickets_and_drafts` |
| Human approval of model writes | IMPLEMENTED for workflow tool calls | IMPLEMENTED AND VERIFIED for drafts: approval bound to a content hash, lock-serialized, idempotent | `test_draft_is_not_a_ticket_until_approved_and_replay_is_idempotent`, `test_concurrent_approvals_create_one_ticket`, `test_changed_draft_requires_a_fresh_review`, `test_rejected_draft_cannot_be_approved`, `test_duplicate_draft_calls_are_deduplicated` |
| Embeddings | PARTIAL: lexical hashing embedder only; `kb.embedder` was stored but ignored at query time | IMPLEMENTED AND VERIFIED: an embedder registry; bge-small-en-v1.5 (fastembed/ONNX, 384 dimensions); each KB is queried with its own embedder | `retrieval/embeddings.py`; `scripts/retrieval_eval.py` runs |
| RAG with citations | PARTIAL: citation-ID verification in the RAG workflow step, no semantic support check | IMPLEMENTED AND VERIFIED for ID validity per run. Semantic support: human rubric written, review **NOT RUN** | `assistant/citations.py`, `test_fabricated_citation_is_removed_and_recorded`; `evals/assistant/GROUNDING_RUBRIC.md` |
| Measurable retrieval | PARTIAL: a benchmark existed on another fixture | IMPLEMENTED AND VERIFIED: hit rate @3 and search latency across 2 chunkings × 2 embedders × 2 strategies, on labelled dev questions | `evals/retrieval/1016-150627/`, `evals/retrieval/1016-150734/` |
| Safe re-ingestion, stable IDs | PARTIAL: content-hash deduplication only | IMPLEMENTED AND VERIFIED: `doc_id` front matter, `doc_id#ordinal` chunk IDs, a changed doc replaces its old version | `test_changed_document_replaces_its_previous_version`, `test_demo_setup_is_idempotent_and_needs_write_access` |
| Prompt-injection handling | PARTIAL: a request guard on input, no retrieval-side tests | IMPLEMENTED: 5 isolated injection docs; tool results treated as data; 5 dev + 2 held-out injection cases. Live resistance **BLOCKED** | `corpus/harbor-pine/injection/`; `evals/assistant/dev.jsonl` |
| Agent-behaviour evals | MISSING: workflow evals existed, but no tool-selection or approval scoring | IMPLEMENTED AND VERIFIED (harness, FIXTURE): 30 dev + 10 held-out cases, 7 dimensions, NOT RUN accounting. Live **BLOCKED** | `scripts/assistant_eval.py`; `evals/assistant/results/*`; `tests/unit/test_assistant_evals.py` |
| Model comparison | MISSING | Runner IMPLEMENTED with request and spend caps. Run **BLOCKED** | `evals/assistant/results/1016-152253-dev/` (all NOT RUN) |
| Instrumentation (run ID, tokens, cost, latency) | PARTIAL: usage ledger per workflow call; stale prices; no 1-hour cache writes or long-context tier | IMPLEMENTED AND VERIFIED: run/event/usage linkage; cache token categories; prices for Claude 5.5 models with source and date; unknown ≠ 0 | `llm/pricing.py`; `test_one_hour_cache_writes_priced_separately`, `test_haiku_5_5_long_context_tier_applies_to_whole_request`, `test_cost_is_unknown_not_zero_when_nothing_ran` |
| One measured improvement | n/a | IMPLEMENTED AND VERIFIED: semantic dense retrieval, hit rate @3 11/12 → 12/12, with one fixture-level grounding regression reported | `docs/ASSISTANT_BENCHMARKS.md` §2 |
| ReAct paper note and experiment | MISSING | Note written. Both experiment arms implemented and harness-checked. Live **BLOCKED** | `docs/REACT_NOTE.md`; `evals/assistant/results/1016-152137-dev-fw/` |
| Reproducible demo and deployment | IMPLEMENTED for the platform | Updated: `.env.example`, `render.yaml` (server-side key, budget), corpus in the image, demo-setup endpoint, demo script | `docs/ASSISTANT.md`, `docs/DEMO_SCRIPT.md`; `test_env_example_loads_as_is` |
| Tester feedback | MISSING | Template only. **No testers yet**; none invented | `docs/FEEDBACK_TEMPLATE.md` |

## Not verified in this audit

- **Live Anthropic calls:** no `SF_ANTHROPIC_API_KEY` in the environment.
- **PostgreSQL + pgvector:** checked locally in a throwaway `pgvector/pgvector:pg16`
  container (Docker), not only on SQLite.
  - `test_assistant.py`: **26 passed**, including concurrent approvals under real row locks.
  - Knowledge, platform (incl. migration drift) and security suites: 239 passed. They ran
    together with the assistant tests that were failing at the time.
  - The first run found a real bug that SQLite hid: the demo status `waiting_on_customer`
    overflowed `VARCHAR(16)`. It is fixed, and a guard test now catches it on SQLite.
  - The rest of the 734-test suite was **not** re-run on PostgreSQL locally; CI covers it.
- **Deployed demo:** the Render deployment has not been updated with this branch.
  Deploying needs the account owner's go-ahead.
