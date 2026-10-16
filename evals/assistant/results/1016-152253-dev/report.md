# Assistant eval: dev.jsonl (BLOCKED)

| Setting | Value |
|---|---|
| started | 2026-10-16T15:22:53+00:00 |
| models | anthropic:claude-haiku-5-5, anthropic:claude-sonnet-5-5 |
| tool_mode | model |
| embedder | hashing-v1 |
| chunking | 800/120 |
| top_k | 4 |
| retrieval_strategy | hybrid |
| prompt_version | assistant-v1 |
| corpus_version |  |
| caps | requests=400 spend_usd=2.0 (used: 0 requests, $0.0000) |
| database | SQLite (throwaway) |
| label | model comparison: BLOCKED (no API key) |

## Summary

| Model | Pass | Fail | Not run | Pass rate | Median latency (ms) | p95 latency (ms) | n | Total cost | Avg cost / attempt | Cost / success | Failed runs | Limit reached |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| anthropic:claude-haiku-5-5 | 0 | 0 | 30 | — | — | — | 0 | unknown | unknown | unknown | 0 | 0 |
| anthropic:claude-sonnet-5-5 | 0 | 0 | 30 | — | — | — | 0 | unknown | unknown | unknown | 0 | 0 |

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
| answerable | 0 / 0 | 12 |
| tool | 0 / 0 | 8 |
| unanswerable | 0 / 0 | 5 |
| injection | 0 / 0 | 5 |

### anthropic:claude-sonnet-5-5: dimensions

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
| answerable | 0 / 0 | 12 |
| tool | 0 / 0 | 8 |
| unanswerable | 0 / 0 | 5 |
| injection | 0 / 0 | 5 |

## Cases

| Model | Case | Verdict | Run status | Reasons |
|---|---|---|---|---|
| anthropic:claude-haiku-5-5 | dev-ans-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-06 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-07 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-08 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-09 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-10 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-11 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-ans-12 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-06 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-07 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-tool-08 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-una-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-una-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-una-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-una-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-una-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-inj-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-inj-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-inj-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-inj-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-haiku-5-5 | dev-inj-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-06 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-07 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-08 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-09 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-10 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-11 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-ans-12 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-06 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-07 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-tool-08 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-una-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-una-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-una-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-una-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-una-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-inj-01 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-inj-02 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-inj-03 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-inj-04 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
| anthropic:claude-sonnet-5-5 | dev-inj-05 | NOT RUN | — | BLOCKED: no Anthropic API key configured |
