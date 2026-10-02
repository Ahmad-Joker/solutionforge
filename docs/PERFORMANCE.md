# Performance and load testing (Phase 17)

**Measured on 2026-10-02 on a laptop, not a server.** Read these numbers as the behaviour
of the design under load and as regression baselines. They aren't production capacity: a
real deployment has dedicated cores and runs the load generator elsewhere.

## Setup

| | |
|---|---|
| **Machine** | Intel Core i7-10510U (4 cores / 8 threads, 15 W, 1.8 GHz base), 16 GB RAM, Windows 11, Docker Desktop 29.6 (8 vCPU, 7.7 GB) |
| **Contention** | The host used ~27% CPU *before* the test (desktop apps), and the load generator ran on the same machine |
| **Stack** | `docker compose`: PostgreSQL 16 + pgvector, Redis, API with **4 uvicorn processes**, **2 worker containers**. Mock LLM (no network calls) |
| **Tool** | k6 1.3.0 (`load/k6/mix.js`, `load/run.sh`). Open-model arrival rates: load doesn't slow down when the server does |
| **Mix ("steady", 1×)** | Each second: 40 read iterations (list workflows, list executions, get execution = 120 GETs), 5 workflow starts, 10 hybrid searches ≈ **135 req/s** |
| **SLO used** | p95 < 300 ms for reads, < 500 ms for writes and search; < 1% errors |
| **Repeats** | 3 runs per rate (6 at full rate; 2 at 0.75×). Cells show the median across runs (min–max) |

## Results

### Rate 0.5× (≈ 68 req/s): SLO met in every run

| endpoint | p50 ms | p95 ms | p99 ms |
|---|---|---|---|
| list_workflows | 11 (11–11) | 73 (31–103) | 707 (148–1020) |
| list_executions | 10 (9–11) | 58 (26–92) | 411 (123–552) |
| get_execution | 10 (10–11) | 65 (28–95) | 427 (126–592) |
| start_execution | 20 (19–20) | 100 (55–190) | 1297 (157–1529) |
| search_hybrid | 22 (19–23) | 102 (56–112) | 776 (335–1152) |

Throughput 67.6 req/s, **0.00% errors**, 0–1 dropped iterations.

### Rate 0.75× (≈ 100 req/s): at the SLO boundary (the knee on this machine)

| endpoint | p50 ms | p95 ms | p99 ms |
|---|---|---|---|
| list_workflows | 12 (11–14) | 352 (286–417) | 1144 (1138–1150) |
| list_executions | 12 (11–12) | 233 (212–253) | 812 (791–833) |
| get_execution | 13 (11–14) | 221 (209–234) | 816 (765–866) |
| start_execution | 29 (26–32) | 497 (311–683) | 1450 (1104–1797) |
| search_hybrid | 27 (23–31) | 463 (189–738) | 1521 (935–2107) |

Throughput 100.7 req/s, 0.00% errors.

### Rate 1× (≈ 132 req/s): saturated

Six runs. p50 74–152 ms (35–1140); **p95 1.2–1.8 s** (0.7–6.1 s); 0.5% errors in the worst
run. All four API processes were pinned at ~100% CPU. The run-to-run spread is the
laptop: it's past its CPU budget, and thermal and background load decide the tail.

### End-to-end workflow latency (0.5×, 365 executions, all succeeded)

Measured from database timestamps, from "start execution" accepted to "finished". The
workflow is a tool step plus an LLM step.

| | p50 | p95 | p99 |
|---|---|---|---|
| queue wait (created → picked up) | 365 ms | 953 ms | — |
| end to end | 820 ms | 2.36 s | 3.32 s |

Queue wait is dominated by the worker **poll interval** (1 s by default). `LISTEN/NOTIFY`
wake-ups are already in the backlog and would remove most of it.

### Overload (stress: reads ramped to 400 iterations/s ≈ 3× the knee)

- About **190 req/s** served.
- The excess was shed as fast **503 `server_busy` responses with `Retry-After` (4.4%)**.
- **No 500s, no crash, no lock-up.** `/readyz` was healthy immediately afterwards.
- The p95 under overload (~10 s) is the configured pool wait (`SF_DATABASE_POOL_TIMEOUT_SECONDS`).

## What load testing found and fixed

The first run was **21.4 req/s with 31.7% errors** and p95 ≈ 30 s. Four problems, each
fixed and verified:

| # | Finding | Evidence | Fix |
|---|---|---|---|
| L1 | **Connection-pool deadlock in knowledge-base search.** The request held its connection (open read transaction after the permission check) while the retriever waited for a second one. At 10 searches/s all 30 connections ended up holding one and waiting for another, and **every endpoint stalled**. | Postgres showed 30 connections `idle in transaction`, all after the same KB lookup, with CPU nearly idle; 2,956 `QueuePool limit … reached` errors | The request's transaction is ended before retrieval. Regression test: concurrent searches against a **one-connection pool**, which fails on the old code (all 503) and passes now |
| L2 | **Pool exhaustion surfaced as 500s after 30 s.** | Logs | It now returns `503 server_busy` + `Retry-After: 1` after `SF_DATABASE_POOL_TIMEOUT_SECONDS` (default 10). Tested |
| L3 | **One uvicorn process, one core.** | API pinned at 100% of 1 core; Postgres ~21% | `WEB_CONCURRENCY` worker processes. Connection budget sized per process. Prometheus **multiprocess mode**, so `/metrics` aggregates all processes instead of reporting whichever one answered |
| L4 | **Per-process dev secrets.** With no `SF_JWT_SECRET`, each process minted its own key: **15,504 401s**, about 3 of 4 requests. Latent since Phase 13: the API and worker *containers* also had different credential keys, so tools couldn't decrypt connector credentials saved through the API. | Access logs; key hashes per container | A one-shot `dev-secrets` compose service shares generated keys, and the start script refuses multi-process mode without secrets. Verified: identical key hashes in all containers, and an approval-gated email sent by a worker using an API-stored credential |

Also removed after profiling: search no longer loads the 1024-float embedding of every
hit, and the executions list no longer loads per-step outputs. The profile was otherwise
flat (framework, driver and validation overhead), so the lever is processes and replicas,
not a hotspot.

**After the fixes:** 0% errors at every rate up to saturation, the SLO met at ~68–100 req/s
on this laptop, and graceful load shedding beyond that.

## Reproduce

```bash
cp .env.example .env
docker compose up -d --build --wait api worker
load/run.sh steady                       # 1× mix, 3 min
RATE_SCALE=0.5 load/run.sh steady        # half rate
DURATION=1m load/run.sh stress           # overload ramp
python load/report.py load/results/<file>.json
python load/compare.py load/results/*steady*.json
python load/probe.py                     # one-at-a-time latency per endpoint
```

Raw k6 summaries for every run above are in `load/results/`, with tokens stripped.

## Not measured yet

- **A real server or the free Render deployment.** The free tier gets a fraction of a CPU
  plus cold starts, and is intended for demos, not load.
- **Real LLM latency.** The mock answers instantly. With a hosted model, end-to-end
  latency is dominated by the provider, and the workers' concurrency becomes the knob.
- **Larger corpora.** pgvector HNSW and full-text search were only measured on the 12-doc
  benchmark corpus here.
