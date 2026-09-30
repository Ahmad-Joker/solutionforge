# ADR-0005: Deterministic authorization and token design

- Status: Accepted
- Date: 2026-10-01

## Decision
- **RBAC** is a static role → permission matrix in code (`security/rbac.py`), unit-tested for
  monotonicity (higher roles strictly include lower ones). Checks happen in the **service
  layer**, so API routes, workers and evaluation runs are all subject to them.
- **Agents never authorize.** An LLM can propose a tool call. The policy engine decides from
  the tool's declared risk level, tenant policy and caller role. Prompts contain no security
  rules that matter.
- **Access tokens:** HS256 JWT, 15 minutes, carrying only the user ID (`sub`), with pinned
  algorithm, `aud`/`iss`/`typ` checks. Org and role are loaded from the DB on every request,
  so removing a member takes effect immediately.
- **Refresh tokens:** opaque, 256-bit, stored hashed, rotated on every use, grouped in families.
  Replaying a rotated token revokes the family (theft detection).

## Consequences
- One indexed membership lookup per tenant request. That's cheap, and a Redis cache is
  possible later with explicit invalidation.
- Moving to asymmetric signing (EdDSA/RS256 + JWKS) is straightforward if other services need
  to verify tokens.
