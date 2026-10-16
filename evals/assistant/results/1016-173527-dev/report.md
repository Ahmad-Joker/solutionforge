# Assistant eval: dev.jsonl (BLOCKED)

| Setting | Value |
|---|---|
| started | 2026-10-16T17:35:27+00:00 |
| models | anthropic:claude-haiku-5-5 |
| tool_mode | model |
| embedder | hashing-v1 |
| chunking | 800/120 |
| top_k | 4 |
| retrieval_strategy | hybrid |
| prompt_version | assistant-v1 |
| corpus_version |  |
| caps | requests=15 spend_usd=0.05 (used: 0 requests, $0.0000) |
| database | SQLite (throwaway) |
| label | live smoke attempt: BLOCKED (no key) |

## Summary

| Model | Pass | Fail | Not run | Pass rate | Median latency (ms) | p95 latency (ms) | n | Total cost | Avg cost / attempt | Cost / success | Failed runs | Limit reached |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| anthropic:claude-haiku-5-5 | 0 | 0 | 3 | — | — | — | 0 | unknown | unknown | unknown | 0 | 0 |

Latency percentiles use nearest rank. With n ≤ 30 the p95 is close to the maximum
and is not a stable estimate.

Costs are **estimates** from list prices ([source](https://platform.claude.com/docs/en/about-claude/pricing), accessed 2026-10-16), not an invoice.

### anthropic:claude-haiku-5-5: dimensions

| Dimension | Passed / applicable |
|---|---|
| retrieval | 0 / 0 |
| tool_selection | 0 / 0 |
| argument_validity | 0 / 0 |
| task_completion | 0 / 0 |
| grounded_citations | 0 / 0 |
| abstention | 0 / 0 |
| approval_enforcement | 0 / 0 |
| semantic grounding (human review) | NOT RUN |

| Category | Passed / ran | Not run |
|---|---|---|
| answerable | 0 / 0 | 3 |
| tool | 0 / 0 | 0 |
| unanswerable | 0 / 0 | 0 |
| injection | 0 / 0 | 0 |

## Cases

| Model | Case | Verdict | Run status | Reasons |
|---|---|---|---|---|
| anthropic:claude-haiku-5-5 | dev-ans-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
