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
| T8 | Credential brute force | High-rate login attempts | Failed logins audited | ✅ Fixed-window limits: login per account (10/5 min) and per IP (50/5 min), registration and refresh per IP; Redis-backed when configured (fails open on a Redis outage, logged); 429 + Retry-After (tested) |
| T9 | Hash-flood DoS | Very large password input | 128-char cap in the schema, enforced before hashing | ✅ |
| T10 | Audit tampering | App bug or compromised DB writer | Insert-only service API; PG trigger rejects UPDATE/DELETE | ✅ |
| T11 | Secret leakage via logs | Tokens and passwords in metadata or errors | Metadata redaction; validation errors drop `input`; opaque 500s; request-ID header sanitized | ✅ |
| T12 | Prompt injection → unauthorized action | Malicious user input or retrieved document tells the agent to call a tool | Tools must be installed per tenant; arguments are schema-validated (extra fields rejected, strings bounded, single-line subjects); a deterministic risk gate blocks external and high-risk actions | 🟡 Approvals for external and high-risk actions (P8) ✅. Agents: allowlist enforced in code, tool output marked as untrusted, and a test where the model *obeys* an injected instruction still produces no refund or email. Remaining: role-aware policy P7, approvals P8, full adversarial suite P16 |
| T13 | Invented citations | Model fabricates sources | Label-enum schema plus code verification of citations and inline markers; labels map to retrieved chunk IDs and offsets; no sources → no model call (tested with invented and mismatched citations) | ✅ |
| T14 | PII exfiltration via tools/LLM | Agent asked to dump customer data | Tool output filtering, per-tool data scopes, adversarial eval set | ⬜ Phase 16 |
| T15 | Runaway cost / infinite loops | Agent loops, huge contexts | Step/time budgets (P2); per-call worst-case pre-check against org daily/monthly, per-execution cost and token limits (P3); a budget stop can't be bypassed via `on_error` | ✅ (agent loop bounds: P5) |
| T16 | Connector credential theft | DB read access, API, logs | Fernet-encrypted at rest with a rotatable key ring (`SF_CREDENTIALS_KEYS`, required in prod); write-only API (`has_credentials` flag only); audit records field names, never values | ✅ |
| T18 | Code execution via workflow definitions | A malicious admin or a compromised account submits a definition | No eval: a closed expression language (references, templates, 11 predicates, no calls or regex); step types come from a server-side registry; configs are schema-validated | ✅ |
| T19 | Split-brain workers double-applying progress | GC pause or partition past the lease | Fenced writes (`lease_owner = me`), per-step leases, unique `(execution_id, seq)`; side-effect steps receive an idempotency key | ✅ |
| T20 | Execution history tampering | Rewriting step records after the fact | Engine only inserts; PG trigger rejects UPDATE on `execution_steps` | ✅ |
| T21 | Resource exhaustion by definitions or inputs | Infinite loops, huge state or inputs | `max_steps`, `max_active_seconds`, per-step timeouts, 256 KB input and step-output caps, 1 MB state cap, ≤ 200 steps per definition | ✅ |
| T22 | LLM provider key leakage | Logs, API responses, errors | `SecretStr` settings (masked in repr/dumps); the key is only passed to the SDK client; provider error messages are replaced with our own | ✅ |
| T23 | Customer data copied into logs and ledgers | Prompts in logs or usage rows | Structured logs carry metadata only (model, tokens, cost, latency); `usage_records` has no content columns (tested) | ✅ |
| T24 | Unmetered or unbounded spend via config | Unknown model, huge `max_tokens` | Unpriced models are rejected at compile time; `max_tokens` ≤ 64k; SDK retries disabled so every billed attempt is recorded | ✅ |
| T25 | Tool argument injection | Header injection, SQL-ish payloads, unexpected fields | Pydantic models with patterns and `extra="forbid"`; parameterized queries only; a property test checks every string is bounded | ✅ |
| T26 | Duplicate side effects | Retries, crash recovery, concurrent calls | Per-visit idempotency keys, executor replay ledger, connector-level unique keys; non-deduplicating writes are never retried | ✅ |
| T27 | Cross-tenant data through tools | Tool reads another org's records, replays another org's key | Tools receive only their org ID and every query filters on it; ledger keys are scoped per org (tested) | ✅ |
| T28 | Agent runaway / loops | Model repeats calls or never finishes | `max_turns`, `max_tool_calls`, repeat detection, budgets on every turn; invalid actions fail closed | ✅ |
| T29 | Idempotency key misuse | Same key, different payload | Executor rejects mismatched args on replay (`tool_idempotency_conflict`) | ✅ |
| T30 | Cross-tenant retrieval | Same KB name in another org; victim KB or doc IDs in own-org paths | Every query filters org + KB in SQL; steps resolve KB names within their own org only (tested, incl. same-name KBs) | ✅ |
| T31 | Poisoned documents / injection via sources | A retrieved text contains instructions | Sources are delimited and marked untrusted; `grounded_answer` has no tools; agents act only through the policy gate | 🟡 (adversarial corpus P16) |
| T32 | Stale authority in long-running workflows | User demoted or removed after starting an execution | Initiator's role re-resolved per tool step; policy denies if they're no longer a member or lack the permission (tested) | ✅ |
| T33 | Org-wide kill switch for risky tools | Incident response needs to stop a tool now | `PUT /tool-policy` blocks tools or risk levels for all workflows immediately; audited | ✅ |
| T34 | Approval bypass / self-approval | Resume endpoint misuse, approving one's own high-risk request, stale approval reused | Decisions only via the approvals API (resume refuses tool approvals); approver permission from policy; four-eyes for high risk; approvals bound to one step visit; policy re-checked at execution (tested) | ✅ |
| T35 | Token theft via XSS | Script injection in the dashboard | Tokens only in httpOnly cookies set by the BFF; JSON rendered as text (React escaping); `X-Frame-Options: DENY`, `nosniff` | ✅ |
| T36 | CSRF against the BFF | Cross-site form posts using the session cookie | SameSite=Lax + mandatory `x-sf-csrf` header on mutating proxy calls (custom headers can't be sent cross-site without CORS, which isn't enabled) (verified) | ✅ |
| T17 | Supply-chain vulnerabilities | Vulnerable dependencies | `pip-audit` in CI; pinned base images | 🟡 |

## Known gaps (tracked in the backlog)

- Registration returns 409 for existing emails, which enables enumeration. The planned
  mitigation is email verification, with a uniform "check your inbox" response.
- PostgreSQL Row-Level Security is not enabled yet (application-level scoping only). The reasoning and plan are in ADR-0011.
- The API still returns refresh tokens in JSON for API clients. The dashboard never exposes them (BFF with httpOnly cookies).
- Per-IP rate limits behind the BFF rely on `X-Forwarded-For`. The edge load balancer must overwrite it, and the API must trust it only from the BFF (`FORWARDED_ALLOW_IPS`).
- Invitation tokens are shown to the inviting admin because there is no email delivery yet.

## Reporting

Please report vulnerabilities privately via GitHub Security Advisories rather than public issues.
