# CV bullets (evidence-based)

Three separate groups. Don't merge them in a CV line. In particular, don't imply that a
live model was evaluated: the project budget was $0 and no paid Claude calls were made.

## Implemented and tested (software)

- **Built a tool-calling support assistant (Python/FastAPI, Next.js) on the Anthropic
  Python SDK's native tool-use interface.**
  - Three allowlisted tools; arguments validated in code; permissions checked against the
    user, not the model.
  - Limits on turns, tool calls, time and per-run spend, plus bounded retries.
  - Tested through the real SDK over a mocked HTTP transport, with 26 integration tests and
    a Playwright browser test. Deployed to Render with a labelled rule-based test policy in
    place of a model.
- **Designed human approval for AI-drafted support tickets.**
  - The model can only create drafts.
  - Approval requires a staff permission and is bound to a SHA-256 hash of the reviewed
    text.
  - A row lock and an idempotency key prevent duplicates. Verified on PostgreSQL: 5
    concurrent approvals create exactly one ticket.
- **Wrote an agent-behaviour eval harness:** 30 dev and 10 held-out cases, 7 scoring
  dimensions. It reports PASS, FAIL and NOT RUN separately, labels rule-based fixture runs,
  and enforces request and spend caps.
- **Implemented LLM cost accounting.** It covers both cache-write durations, a long-context
  price tier and failed attempts, and never reports unknown cost as zero. Unit-tested.
- **Set up CI with six required jobs:** strict typing including the optional
  semantic-search dependency, a base install without it, the PostgreSQL suite with
  coverage gates, a no-skip security suite, reversible migrations, browser tests and
  Docker builds.

## Measured (real runs, no paid model calls)

- **Compared retrieval configurations** on 12 labelled questions, on SQLite and on
  PostgreSQL + pgvector. Semantic dense retrieval (bge-small, ONNX) reached a hit rate @3
  of 12/12, against 11/12 for the lexical default. pgvector median search was 27 ms vs
  54 ms.
- **Measured memory in a 512 MB container across repeated runs.**
  - Lexical: ~153 MiB peak.
  - Semantic: ~318 MiB typical peak, one 475 MiB outlier, ~298 MiB resident.
  - Conclusion: not recommended for Render's free tier.
- **Found and fixed a container bug.** The non-root API user couldn't write the model
  cache, so every semantic ingestion failed. The fix bakes the model into the image.
- **Measured prompt-injection exposure by retrieval config (rankings only).** Semantic
  dense put injected passages in 11 of 20 top-4 slots, against 7 of 20 for the lexical
  default. Kept the lexical default because of it.

## Not verified (state plainly if asked)

- **No live Claude evaluation was run** ($0 budget). Unknown: answer quality, tool-selection
  accuracy, injection resistance, live latency and cost.
- **The model comparison and the ReAct fixed-vs-model-selected experiment: NOT RUN.** The
  harness for both exists and was checked with the fixture policy only.
- **The production smoke test with a signed-in account was not run by me.** The script is
  ready (`scripts/assistant_smoke.py`). Only unauthenticated checks of the deployment were
  made.
- **Semantic grounding (whether cited text supports each answer) had no human review.**
