# ADR-0011: Initiator-aware tool policy, rate limiting, and deferring Postgres RLS

- Status: Accepted
- Date: 2026-10-01

## Decisions

**1. Tool calls are authorized as the execution's initiator, resolved at call time.** The
engine looks up the creator's *current* membership role when each step starts. The tool
policy denies a call when:

- the initiator is no longer a member;
- the initiator's role lacks the tool's `required_permission`;
- the organization's `ToolPolicyConfig` blocks the tool or its risk level.

A user demoted or removed after starting a long-running workflow can't keep acting through
it (tested).

**2. `required_permission` is the requester's permission.** Approvers are a separate decision
(`approver_permission` from the policy). Phase 4 had conflated the two for email and refunds.

**3. Rate limiting.** Fixed windows on login (per account and per IP), registration and
refresh. Redis is used when configured (shared across replicas), with an in-memory fallback.
A Redis outage **fails open**: login still needs the password, and locking every user out
during an incident is worse. That trade-off is explicit and logged. Keys are hashed.

**4. Postgres Row-Level Security is deferred, deliberately.** RLS done properly means:

- a non-owner DB role (or `FORCE ROW LEVEL SECURITY`);
- `SET LOCAL app.org_id` on every tenant transaction;
- an explicit bypass role for system work: claim loops, ingestion, membership resolution
  across orgs.

That touches every session path and can only be validated against a real PostgreSQL, which
this development environment hasn't had. Shipping it unverified would add risk, not remove
it. Today's isolation is application-level (`TenantContext` + `scoped_select`) with
parametrized cross-tenant tests on every route. RLS is planned once the PostgreSQL CI and
compose environment is exercised (Phase 13).

## Consequences
- An extra indexed membership query per tool-bearing step.
- With in-memory limiting, N API replicas allow N× the limit. Production sets `SF_REDIS_URL`.
