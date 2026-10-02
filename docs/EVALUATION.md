# Evaluation and quality-gated deployment

An evaluation **runs a dataset of cases against one pinned workflow version through the real
engine**: same tools, policy gate, approvals, budgets and metering as production. Every score
is computed by code from what the run actually did. Nothing here asks a model to grade a
model.

## Concepts

| Object | What it is |
|---|---|
| **Dataset** | Named set of cases for one workflow (`evaluation_datasets`). |
| **Case** | An `input` plus `expectations` and `tags`. A case tagged `security` counts toward the security metric. |
| **Run** | One dataset × one workflow version. Each case runs as a normal execution tagged with the run and case IDs. |
| **Result** | Per case: passed or failed, the individual checks, the failure reasons, and a link to the execution that produced it. |
| **Gate policy** | Per-workflow rules that a version must pass before it can become production. |
| **Deployment decision** | Every gated deploy attempt (allowed, blocked or overridden) and the checks that decided it. |

## Case expectations (all optional)

```json
{
  "status": "succeeded",                // succeeded | failed | waiting | budget_exceeded
  "output_subset": {"tier": "gold"},    // deep subset match against the execution output
  "output_schema": {"type": "object"},  // JSON Schema the output must satisfy
  "expected_tools": ["crm.get_customer"],
  "forbidden_tools": ["payments.issue_refund"],  // attempted = executed, denied, or held for approval
  "expected_cited_titles": ["Refund policy"],
  "max_steps": 5,
  "max_cost_usd": "0.002"
}
```

Unknown keys are rejected, so a typo can't silently disable a check.

## Metrics per run

| Metric | Definition |
|---|---|
| `pass_rate` | Share of cases where every applicable check passed. |
| `tool_selection_accuracy` | Share of cases *that declare tool expectations* where expected tools were attempted and no forbidden tool was. |
| `structured_output_validity` | Share of cases *with an `output_schema`* whose output validates. |
| `citation_accuracy` | Share of cases *with grounded answers or expected citations* where every citation points at a chunk the run actually retrieved and every expected title is cited. |
| `groundedness_lexical` | Mean share of answer sentences (≥3 content words) whose words are ≥60% present in the retrieved text. **A lexical proxy, not semantic entailment.** Abstentions are excluded. |
| `latency_p50_ms` / `latency_p95_ms` | Nearest-rank percentiles of execution active time. These are always observed values, never interpolated. |
| `cost_per_case_usd` | Metered LLM spend ÷ cases, from `llm_usage` rows. |
| `steps_mean`, `error_rate` | Mean steps; share of cases that ended with an error code. |
| `security_cases` / `security_cases_passed` | Counts over cases tagged `security`. |

A metric that no case measures is reported as `null` ("n/a" in the UI), **not as 100%**.

## Lifecycle

1. `POST /evaluation/datasets/{id}/runs {version}`. Each valid case is queued as an
   execution; a case whose input the workflow rejects is recorded as failed immediately.
2. The worker runs the executions like any others.
3. A background loop (every 3 s) finalizes a run when all its executions are terminal or
   waiting for a human, or when its 15-minute deadline passes. It scores each case, then
   **cancels any execution still waiting or queued**, which also cancels its pending
   approvals. An evaluation never leaves an approval request in someone's inbox, and never
   sends anything after it ends.

## Deployment gate

`PUT /workflows/{id}/gate` (requires `workflow:deploy`):

```json
{
  "dataset_id": "…",
  "no_pass_rate_regression": true,      // candidate pass_rate ≥ production's
  "min_pass_rate": 0.9,
  "min_citation_accuracy": 0.95,        // fails if the dataset doesn't measure it
  "min_tool_selection_accuracy": 1.0,
  "max_p95_latency_ms": 5000,
  "max_cost_per_case_usd": "0.01",
  "require_security_cases_pass": true
}
```

On `POST /workflows/{id}/deployments` the service:

1. takes the **latest completed run** of the candidate version on the gate dataset (if none
   exists, the `evaluated` check fails);
2. takes the latest completed run of the **current production version** as the baseline;
3. evaluates every check and stores a `DeploymentDecision`, even when the result is a block;
4. on failure returns **409 `deployment_blocked`** with every check and its detail.
   Production is unchanged.

**Break-glass:** an OWNER can deploy past a failed gate with `override_gate_reason`
(minimum 10 characters). The decision is stored as `overridden` and audited as
`workflow.deployment_gate_overridden`. Admins can't override.

Rollbacks are gated too. Rolling back to a version requires it to be evaluated on the gate
dataset, so the claim that it's "known good" is proven again rather than assumed.

## Known limitations (honest list)

- **The gate trusts the latest completed run.** With a non-deterministic model, someone
  could re-run until a lucky pass. Mitigations: decisions record which run they used, and
  all runs are listed. A future option is to require N consecutive runs, or to gate on the
  worst of the last N.
- **Datasets are mutable.** A candidate run and its baseline may have been scored on
  different case sets if cases changed in between. Dataset versioning or snapshotting is in
  the backlog.
- **Evaluations spend real budget** (by design: they meter exactly like production), and
  their executions appear in the executions list.
- **Groundedness is lexical.** A paraphrase of the source can score low, and a sentence that
  reuses source words with a wrong meaning can score high. An NLI- or judge-based scorer
  can be added as a separate, clearly labelled metric. It won't replace the deterministic
  checks.
