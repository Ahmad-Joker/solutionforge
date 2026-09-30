# Security

## Principles

1. **Authorization is deterministic code.** Roles map to permissions in `security/rbac.py`.
   Tool risk levels map to policy decisions in the policy engine (⬜ Phase 7). No prompt, model
   output, or retrieved document can grant a permission.
2. **Tenant isolation by construction.** Tenant-owned data is reachable only through a
   `TenantContext`, which is minted after a membership check. Foreign resources return 404,
   the same response as a missing one.
3. **Secrets are never stored in plaintext.** Passwords use Argon2id. Refresh and invitation
   tokens are stored as SHA-256 hashes. Audit metadata is recursively redacted.
4. **Fail closed.** Unknown token → 401. Missing membership → 404. Missing permission → 403.
   Unhandled error → opaque 500 with a request ID.

## Threat model (v1, STRIDE)

Assets: tenant documents and data, connector credentials, the ability to trigger external
actions, audit history, and model spend.

| # | Threat | Vector | Control | Status |
|---|---|---|---|---|
| T1 | Cross-tenant data access | Guessing or substituting `org_id` or resource IDs | Membership-gated `TenantContext`; `scoped_select`; 404 for foreign resources; parametrized isolation tests over every route | ✅ |
| T2 | Confused deputy | Own org in the path, victim's resource ID in the request | Resource lookups are always scoped to the context org; tested | ✅ |
| T3 | Privilege escalation | Admin grants owner; self-promotion; demoting a higher rank | `can_assign_role` rank rules; last-owner protection with row locks | ✅ |
| T4 | Token forgery | `alg=none`, key confusion, wrong audience/issuer | Pinned HS256, required claims, `aud`/`iss` checks, `typ=access` | ✅ |
| T5 | Stolen refresh token | Replay after theft | Rotation on every use; reuse revokes the whole family; audited | ✅ |
| T6 | Stale authorization | Removed member keeps using a valid JWT | Role resolved from DB per request, not stored in the token | ✅ |
| T7 | Account enumeration | Login responses and timing | Same message for unknown user and wrong password; dummy Argon2 verify | ✅ (registration still reveals existing emails; see Known gaps) |
| T8 | Credential brute force | High-rate login attempts | Failed logins audited | 🟡 rate limiting ⬜ Phase 7 (Redis) |
| T9 | Hash-flood DoS | Very large password input | 128-char cap in the schema, enforced before hashing | ✅ |
| T10 | Audit tampering | App bug or compromised DB writer | Insert-only service API; PG trigger rejects UPDATE/DELETE | ✅ |
| T11 | Secret leakage via logs | Tokens and passwords in metadata or errors | Metadata redaction; validation errors drop `input`; opaque 500s; request-ID header sanitized | ✅ |
| T12 | Prompt injection → unauthorized action | Malicious user input or retrieved document tells the agent to call a tool | Policy engine is outside the model; approvals for external actions; tool argument schema validation | ⬜ Phase 7/8/16 |
| T13 | Invented citations | Model fabricates sources | Citations must map to retrieved chunk IDs; verified in code | ⬜ Phase 6 |
| T14 | PII exfiltration via tools/LLM | Agent asked to dump customer data | Tool output filtering, per-tool data scopes, adversarial eval set | ⬜ Phase 16 |
| T15 | Runaway cost / infinite loops | Agent loops, huge contexts | Step, time, token and cost budgets per execution and org | ⬜ Phase 3/5 |
| T16 | Connector credential theft | DB read access | Envelope-encrypted credentials, never returned by the API | ⬜ Phase 4 |
| T18 | Code execution via workflow definitions | A malicious admin or a compromised account submits a definition | No eval: a closed expression language (references, templates, 11 predicates, no calls or regex); step types come from a server-side registry; configs are schema-validated | ✅ |
| T19 | Split-brain workers double-applying progress | GC pause or partition past the lease | Fenced writes (`lease_owner = me`), per-step leases, unique `(execution_id, seq)`; side-effect steps receive an idempotency key | ✅ |
| T20 | Execution history tampering | Rewriting step records after the fact | Engine only inserts; PG trigger rejects UPDATE on `execution_steps` | ✅ |
| T21 | Resource exhaustion by definitions or inputs | Infinite loops, huge state or inputs | `max_steps`, `max_active_seconds`, per-step timeouts, 256 KB input and step-output caps, 1 MB state cap, ≤ 200 steps per definition | ✅ |
| T17 | Supply-chain vulnerabilities | Vulnerable dependencies | `pip-audit` in CI; pinned base images | 🟡 |

## Known gaps (tracked in the backlog)

- No login rate limiting yet (Redis-backed limiter planned).
- Registration returns 409 for existing emails, which enables enumeration. The planned
  mitigation is email verification, with a uniform "check your inbox" response.
- PostgreSQL Row-Level Security is not enabled yet (application-level scoping only).
- Refresh tokens are returned in JSON. The dashboard will use an httpOnly-cookie
  backend-for-frontend (BFF).
- Invitation tokens are shown to the inviting admin because there is no email delivery yet.

## Reporting

Please report vulnerabilities privately via GitHub Security Advisories rather than public issues.
