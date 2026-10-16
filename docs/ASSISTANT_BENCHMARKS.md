# Assistant benchmarks

Every number below links to the artifact it came from. Results are kept in separate
sections by kind, never mixed in one table:

| Kind | Meaning |
|---|---|
| **MEASURED** | A real component run (embedders, retrieval) on this machine |
| **FIXTURE** | The rule-based `fixture:policy-v1` in place of a model. Checks the harness, not model quality |
| **BLOCKED** | Needs an Anthropic API key, which was not available. Nothing was substituted |

Run date: 2026-10-16. Machine: Windows 11 laptop, Python 3.13. Database: a throwaway SQLite
file, so dense search is brute force in Python, not pgvector. Latencies are therefore not
representative of production PostgreSQL.

## 1. Retrieval (MEASURED)

**Setup.**

- Questions: the 12 answerable dev questions, whose acceptable source documents were
  labelled before any run (`evals/assistant/dev.jsonl`). Held-out questions were not used.
- Metric: **hit rate @3**, the share of questions with ≥1 chunk from an acceptable document
  in the top 3. It is not recall.
- Retrieval latency is the search call alone (query embedding + search + fusion), the
  median of 3 repetitions per question after warm-up. It is reported separately from
  end-to-end latency.

**Chunking: 800/120 vs 400/60** ([artifact](../evals/retrieval/1016-150734/report.md))

| Embedder | Chunking | Chunks | Hit rate @3 | Median ms | p95 ms |
|---|---|---|---|---|---|
| hashing-v1 | 800/120 | 93 | 11/12 | 75.7 | 102.0 |
| hashing-v1 | 400/60 | 94 | 11/12 | 77.6 | 102.0 |
| bge-small-en-v1.5 | 800/120 | 93 | 11/12 | 157.0 | 171.8 |
| bge-small-en-v1.5 | 400/60 | 94 | 11/12 | 152.5 | 166.3 |

These two configurations produce nearly the same chunks. The chunker packs paragraphs
within sections, and this corpus's sections are short. So the comparison can't separate
them.

**Chunking 800/120 vs 200/30, and hybrid vs dense** ([artifact](../evals/retrieval/1016-150627/report.md))

| Embedder | Chunking | Strategy | Chunks | Hit rate @3 | Median ms | p95 ms |
|---|---|---|---|---|---|---|
| hashing-v1 | 800/120 | hybrid | 93 | 11/12 | 74.1 | 88.9 |
| hashing-v1 | 800/120 | dense | 93 | 11/12 | 75.6 | 82.2 |
| hashing-v1 | 200/30 | hybrid | 103 | 11/12 | 95.2 | 117.5 |
| hashing-v1 | 200/30 | dense | 103 | 11/12 | 76.5 | 93.0 |
| bge-small-en-v1.5 | 800/120 | hybrid | 93 | 11/12 | 154.6 | 171.4 |
| bge-small-en-v1.5 | 800/120 | **dense** | 93 | **12/12** | 141.1 | 156.5 |
| bge-small-en-v1.5 | 200/30 | hybrid | 103 | 11/12 | 165.2 | 172.8 |
| bge-small-en-v1.5 | 200/30 | **dense** | 103 | **12/12** | 153.5 | 168.8 |

**Every miss is the same question.** dev-ans-10, "Which towns do you cover?", should
retrieve `HP-SVC-05`, which says "We serve Millhaven, …". The keyword side matches "cover"
to the warranty FAQ's "What is covered?", and in hybrid mode that outvotes the dense match.
Semantic dense-only retrieval finds the service-area document.

**Caveats.**

- n = 12, so one question is 8 points. The gain is a single question.
- The bge-small model weighs ~65 MB, downloaded once.
- Search latency roughly doubles on this SQLite setup. The extra ~70 ms median is
  consistent with embedding the query on CPU, but that step was not timed on its own.

**On PostgreSQL 16 + pgvector** ([artifact](../evals/retrieval/1016-155811/report.md)).
This ran in a local Docker container, after the `HP-SOP-01` status-name fix, so the corpus
fingerprint differs by one document from the SQLite runs.

| Embedder | Chunking | Strategy | Hit rate @3 | Median ms | p95 ms |
|---|---|---|---|---|---|
| hashing-v1 | 800/120 | hybrid | 11/12 | 53.6 | 70.9 |
| hashing-v1 | 800/120 | dense | 11/12 | 28.1 | 46.4 |
| hashing-v1 | 200/30 | hybrid | 10/12 | 50.4 | 62.6 |
| hashing-v1 | 200/30 | dense | 11/12 | 33.6 | 45.4 |
| bge-small-en-v1.5 | 800/120 | hybrid | 11/12 | 33.0 | 47.6 |
| bge-small-en-v1.5 | 800/120 | **dense** | **12/12** | **27.0** | 41.6 |
| bge-small-en-v1.5 | 200/30 | hybrid | 11/12 | 33.0 | 39.9 |
| bge-small-en-v1.5 | 200/30 | dense | 12/12 | 32.3 | 57.2 |

- **Same ranking of configurations as on SQLite.** Semantic dense retrieval is the only
  one at 12/12, and on pgvector it is also the fastest median (27 ms).
- **One new miss:** lexical 200/30 hybrid also misses dev-ans-03 (Saturday hours).
  PostgreSQL's full-text ranking differs from the SQLite keyword fallback.

## 2. One measured improvement: semantic dense retrieval (MEASURED + FIXTURE)

**Change:** knowledge-base embedder `hashing-v1` → `bge-small-en-v1.5`, assistant retrieval
strategy `hybrid` → `dense`. Both are settings (`SF_SEMANTIC_EMBEDDER_ENABLED`,
`SF_ASSISTANT_RETRIEVAL_STRATEGY`). The demo-setup endpoint picks the semantic embedder
when it is enabled.

| | Before (hashing-v1, hybrid) | After (bge-small, dense) | Artifact |
|---|---|---|---|
| Retrieval hit rate @3, 12 dev questions (MEASURED) | 11/12 | 12/12 | [retrieval](../evals/retrieval/1016-150627/report.md) |
| Median search latency, SQLite (MEASURED) | 74 ms | 141 ms | same |
| Median search latency, PostgreSQL + pgvector (MEASURED) | 53.6 ms | 27.0 ms | [pgvector](../evals/retrieval/1016-155811/report.md) |
| Agent eval `retrieval` dimension (FIXTURE) | 16/17 | 17/17 | [before](../evals/assistant/results/1016-150957-dev/report.md), [after](../evals/assistant/results/1016-150924-dev/report.md) |
| Agent eval cases passing (FIXTURE) | 16/30 | 20/30 | same |
| Agent eval `grounded_citations` (FIXTURE) | 9/13 | 8/13 | same |

**What changed, case by case (FIXTURE).**

- Four cases flipped to PASS: dev-ans-01, dev-ans-04, dev-una-03, dev-una-04.
- dev-ans-10 now retrieves the right document. It still fails because the fixture quotes
  the wrong sentence.
- **Regression: dev-inj-04.** With dense retrieval, the injected "Warranty clarification"
  page ranks first, and the fixture cites it instead of the real warranty FAQ.

So the change improves retrieval but makes one adversarial document easier to reach. That
argues for the planned live injection evals before adopting it. The code default stays
lexical + hybrid (no download), and the deployed demo keeps the lexical embedder.

## 2b. Semantic embedder memory under a 512 MB limit (MEASURED)

Measured to decide whether the embedder fits Render's free instance (512 MB). Script:
`infra/scripts/measure_api_memory.sh` (2026-10-16, Docker Desktop on WSL2).

**Setup.**

- Images: the API image built without and with `--build-arg API_EXTRAS="[semantic]"`.
- Container limit: 512 MiB (`--memory 512m`, swap equal to memory, so effectively none).
- Settings: production (standard password hashing), embedded worker, migrations on
  start, PostgreSQL 16 + pgvector in a sibling container.
- Workload: `scripts/assistant_smoke.py`. It registers, loads and indexes the 20 demo
  documents, then makes 7 assistant runs and 5 approval calls.
- Memory: the cgroup v2 `memory.peak` and `memory.current` counters. These include page
  cache, so they are an upper bound on what the process itself needs.

| Image | Embedder | After boot | Peak | After workload | OOM-killed | Smoke checks |
|---|---|---|---|---|---|---|
| base (413 MB image) | hashing-v1 | 88.1 MiB | 153.2 MiB | 90.8 MiB | no | 12/12 |
| semantic (814 MB image, model baked in) | bge-small-en-v1.5 | 86.6 MiB | **317.6 MiB** | 304.3 MiB | no | 12/12 |

**Finding: a container bug, fixed.** The first semantic run failed: every ingestion
dead-lettered with `Permission denied`. The non-root `app` user couldn't write the
Hugging Face download cache.

- That would have broken a semantic deployment on Render.
- The image now bakes the model in at build time (`/opt/models`) and sets
  `HF_HOME=/tmp/huggingface`, so nothing is downloaded at runtime.

**Recommendation.**

- It fits: peak 318 of 512 MiB, about 194 MiB of headroom, for this workload.
- The model stays loaded once used (~215 MiB above the base).
- **Not measured:** concurrent requests, larger corpora, and Render's own accounting.
  Treat the headroom as moderate, not large.
- **Turning it on for Render** needs three things:
  1. an image built with `API_EXTRAS="[semantic]"`. The blueprint currently builds
     without it, and how to pass build arguments through Render was not verified;
  2. `SF_SEMANTIC_EMBEDDER_ENABLED=true`;
  3. a new knowledge base. Existing ones keep the embedder they were created with.
- Until then the live demo stays lexical.

## 3. Agent-behaviour evals

### FIXTURE runs: harness checks, not model results

| Run | Config | Pass / fail / not run | Artifact |
|---|---|---|---|
| Harness check | hashing-v1, hybrid, model-selected tools | 16 / 14 / 0 | [report](../evals/assistant/results/1016-150247-dev/report.md) |
| Before | hashing-v1, hybrid | 16 / 14 / 0 | [report](../evals/assistant/results/1016-150957-dev/report.md) |
| After | bge-small, dense | 20 / 10 / 0 | [report](../evals/assistant/results/1016-150924-dev/report.md) |
| Fixed-workflow arm | hashing-v1, hybrid, `--tool-mode fixed` | 11 / 19 / 0 | [report](../evals/assistant/results/1016-152137-dev-fw/report.md) |

**What these runs show.**

- The runner, scorer and approval checks work end to end on all 30 dev cases.
- `approval_enforcement` passed 4/4 in every fixture run.
- The scorer catches failures. It caught the fixture parroting the injected request for
  card numbers (dev-inj-05), unsupported abstentions, and missing facts.

**What they don't show:** anything about how well a language model answers.

### LIVE runs: BLOCKED

| Model | Pass | Fail | Not run | Reason | Artifact |
|---|---|---|---|---|---|
| anthropic:claude-haiku-5-5 | — | — | 30 | BLOCKED: no API key | [report](../evals/assistant/results/1016-152253-dev/report.md) |
| anthropic:claude-sonnet-5-5 | — | — | 30 | BLOCKED: no API key | same |
| anthropic:claude-haiku-5-5 (smoke, 3 cases) | — | — | 3 | BLOCKED: no API key, no spend cap approved | [report](../evals/assistant/results/1016-173527-dev/report.md) |

These tables are empty because nothing ran, and fixture numbers are never copied into them.

**Planned procedure once a key and budget are approved:**

1. **Smoke run:** 3 cases on Haiku 5.5, capped at 15 requests and $0.05.
2. **Comparison:** Haiku 5.5 vs Sonnet 5.5 on all 30 dev cases, capped at 400 requests and
   $2.00.
3. **ReAct experiment** (§4): one model, `--tool-mode fixed` vs `model`.
4. **Held-out run:** one run of the chosen configuration, logged in `HOLDOUT_LOG.md`.

**Reported per model:**

- pass rate and per-dimension scores;
- median and p95 latency, with n;
- average cost per attempt and cost per successful case;
- failed runs and limit-reached runs.

The targets were fixed in advance in `evals/assistant/README.md`.

**Cost accounting.** Estimates come from list prices (source and date in `pricing.py`), not
invoices. Failed attempts' tokens are included. Cost per success is total spend divided
by passing cases, so failures are paid for. Unknown cost is shown as "unknown", never as 0.

## 4. ReAct-style experiment: BLOCKED

Design and the paper note: [REACT_NOTE.md](REACT_NOTE.md). The harness for both arms is
built and checked with the fixture (the fixed-workflow row above). The live comparison
needs an API key.
