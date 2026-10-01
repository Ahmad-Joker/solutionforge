# ADR-0012: Evaluate through the real engine, score in code, gate in the deploy path

- Status: Accepted
- Date: 2026-10-01

## Context

A workflow version change (prompt, model, tool wiring) can silently regress quality,
safety or cost. The build spec asks for evaluation with v1/v2/v3 comparison and a
deployment gate that stores its decision and blocks regressions.

## Decisions

**1. Cases run as ordinary executions of a pinned version.** There is no separate "eval
runner" with its own tool stubs or policy. Evaluation measures exactly what production
would do: the same policy gate, approvals, budgets and metering. A version that would be
blocked in production is blocked in evaluation too, and that's what the scores reflect.

**2. Scoring is deterministic code.** Checks are derived from execution state, tool-call
rows, usage rows and step outputs. Groundedness is a labelled *lexical* proxy. We don't use
an LLM-as-judge for gating because:

- it adds cost and non-determinism to a release control;
- it can be prompt-injected by the very outputs it judges.

A judge can be added later as an extra, separately named metric.

**3. The gate is enforced inside `workflow_service.deploy`.** It isn't a CI step or a UI
check, so no API path can deploy around it. Every attempt writes a `DeploymentDecision`.
Blocked attempts are committed before the 409, so the evidence survives.

**4. Break-glass is OWNER-only and audited.** Incidents sometimes need a fix shipped before
evaluation can catch up. Requiring a reason and recording it is more honest than a gate
people learn to disable.

**5. Finalization is a background loop with a deadline.** Waiting executions are scored as
waiting and then cancelled, along with their approvals. Evaluation must never leave
side-effect requests pending in a human's inbox.

## Consequences

- Evaluations spend real (metered) budget.
- Two separate code paths can't drift apart, because there is only one.
- Rollbacks need an evaluation run of the target version on the gate dataset.
