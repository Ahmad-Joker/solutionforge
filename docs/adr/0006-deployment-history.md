# ADR-0006: Immutable versions + append-only deployment history

- Status: Accepted
- Date: 2026-10-01

## Context
A change to a workflow must not silently reach production. Every run has to be attributable
to an exact definition, rollback must be instant, and Phase 11 needs somewhere to record why a
deployment was allowed or blocked.

## Decision
- `workflow_versions` rows are immutable. There is no update endpoint. A change means a new
  version, whose canonical definition is stored with a SHA-256 hash.
- `workflow_deployments` is an append-only history. Production is the newest row, so rollback
  means inserting a row that points at an older version. Nothing is ever mutated.
- Executions pin `workflow_version_id` when created. Deploying mid-flight never changes a
  running execution's definition, which also makes the compile cache safe.
- Running a version that isn't deployed requires `workflow:write`. Deploying requires `workflow:deploy`.

## Consequences
- Full provenance: which version ran, who deployed it, when, and why.
- The Phase 11 quality gate becomes a precondition inside `deploy()`, with its results stored
  on the deployment record.
- "Current version" relies on `created_at` ordering. A monotonic sequence is in the backlog.
