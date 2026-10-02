# Customer case studies

Three end-to-end scenarios. Each folder has the workflow definition, an evaluation
dataset, and a write-up with **measured** results.

| Case | Shows | Pass rate (PostgreSQL) |
|---|---|---|
| [support](support/): late-order resolution | Deterministic credit rules, four-eyes approval for money, approval for outbound email, PII redaction, injection-proof routing | 8/8 |
| [research](research/): policy assistant | Hybrid retrieval, citations verified in code, fail-safe to sources, off-topic refusal without a model call | 27/28 |
| [ops](ops/): VIP delay sweep | Facts from systems of record, automatic low-risk writes, human-gated sending, typed inputs | 5/5 |

**Two notes on reading the numbers:**
- **They use the deterministic mock model.** They prove the *platform* behaviour each case
  claims: routing, policy, approvals, redaction, retrieval, refusals. They don't measure
  model writing quality. The workflows name `mock:mock-1`; switch to an `anthropic:*`
  model with an API key, and the same datasets measure that too.
- **CI keeps them honest.** `tests/integration/test_case_studies.py` runs all three on
  every build, on both SQLite and PostgreSQL.

Run one against any deployment:

```bash
python apps/api/scripts/case_study.py support|research|ops <base-url>
```
