# ADR-0004: Tenant isolation strategy

- Status: Accepted (layer 2 pending)
- Date: 2026-10-01

## Context
Every tenant's documents, workflows, executions, logs, evaluation data and credentials must be
inaccessible to other tenants, including through bugs.

## Decision
Shared database, shared schema, `organization_id` on every tenant-owned row, with defense in depth:

1. **Routing:** tenant resources are only addressable as `/orgs/{org_id}/...`.
2. **Context:** `resolve_tenant(org_id, user_id)` checks membership and returns a
   `TenantContext`. It's the only constructor used by the API, and services require one.
3. **Queries:** `scoped_select(Model, ctx, …)` always applies `organization_id = ctx.org`.
4. **Responses:** foreign and non-existent resources both return 404 with the same message.
5. **(Pending) PostgreSQL RLS:** a per-transaction `SET LOCAL app.org_id` plus RLS policies,
   so even a hand-written unscoped query cannot cross tenants.
6. **Tests:** parametrized cross-tenant attacks over every route (`tests/security/`), extended
   with every new resource type.

## Consequences
- Cheap to operate. A new tenant is a row, not a schema or a database.
- Noisy-neighbor risk is handled with per-org budgets and rate limits, not physical isolation.
- Schema-per-tenant or DB-per-tenant can be offered later for enterprise tenants if required.
