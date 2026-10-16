# CV bullets (evidence-based)

Each bullet states only what the repository shows, with a pointer to the evidence. Bullets
about live model quality are deliberately absent until the live evals have run.

- **Built a tool-using support assistant on the Anthropic Python SDK.**
  - Native `tools` / `tool_use` / `tool_result` loop with three allowlisted tools and
    Pydantic-validated arguments.
  - Turn, tool-call, deadline and per-run spend limits, and bounded retries of transient
    errors.
  - Evidence: `apps/api/src/solutionforge/assistant/`; 26 integration tests and a
    Playwright E2E test.
- **Designed human-in-the-loop approval for model-initiated writes.**
  - The model can only draft tickets.
  - Approval needs a staff permission and is bound to a SHA-256 hash of the reviewed
    content.
  - Concurrent or repeated approvals create exactly one ticket. Tested under four
    concurrent requests.
- **Added semantic retrieval with measured gains.**
  - bge-small-en-v1.5 embeddings via ONNX, stored in pgvector, with a per-knowledge-base
    embedder registry.
  - Compared 2 chunkings × 2 embedders × 2 strategies on labelled questions. Dense
    semantic retrieval raised hit rate @3 from 11/12 to 12/12.
  - Also reported the adversarial-document regression it introduced.
- **Wrote an agent-behaviour eval suite: 30 dev and 10 held-out cases.**
  - Covers answerable, ticket and tool, unanswerable, and prompt-injection cases.
  - Scores 7 dimensions separately: retrieval, tool selection, argument validity, task
    completion, citation validity, abstention and approval enforcement.
  - Records PASS, FAIL and NOT RUN separately, under spend and request caps.
  - Targets were fixed before any run.
- **Instrumented LLM cost and latency per run.**
  - Token accounting across all attempts, including the 5-minute and 1-hour cache-write
    categories and a long-context price tier.
  - List-price estimates with source and date. Unknown cost is never reported as zero.
  - Retrieval and tool latency are recorded separately.
- **Read and applied the ReAct paper (Yao et al., ICLR 2023).**
  - Wrote a critical note.
  - Built a fixed-workflow vs model-selected-tools comparison harness for this application.
    The live run is pending an API key.

Python · FastAPI · SQLAlchemy · PostgreSQL/pgvector · Anthropic SDK · fastembed/ONNX ·
Next.js/TypeScript · pytest · Playwright
