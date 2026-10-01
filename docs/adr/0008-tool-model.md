# ADR-0008: Typed tools, risk levels, and layered idempotency

- Status: Accepted
- Date: 2026-10-01

## Context
Agents and workflows take real actions: tickets, emails, refunds. Bad arguments, prompt
injection, retries and crashes must not cause unauthorized or duplicated side effects.
Tools should also be exposable to MCP clients and LLM tool use without hand-written schemas.

## Decision
- **Typed contracts.** Inputs and outputs are Pydantic models that forbid unknown fields and
  bound every string. A test enforces this for every tool. Arguments are validated before
  policy and before any side effect. Rejected values are never echoed back.
- **Risk is declared by the tool author, not inferred.** A pure policy function maps
  risk × tenant installation to allow / require-approval / deny. Approval-required actions
  are refused until Phase 8 provides durable approvals, so nothing sensitive ever runs
  unreviewed in the meantime.
- **Opt-in per tenant.** A tool must be installed and enabled for an organization. Credentials
  are Fernet-encrypted with a rotatable key ring, write-only via the API, and audited by
  field name only.
- **Layered idempotency.** The key comes from the engine (stable per step *visit*), is
  deduplicated by the executor's call ledger, and is enforced again by write connectors.
  Retries are allowed only where repeating is safe.
- **Simulated systems are real services.** They're tenant-scoped tables with business rules
  behind the same interface a real connector would use, not mocks. They're replaceable
  per tool.

## Consequences
- Adding a real connector (Zendesk, SendGrid, Stripe) means one class with the same spec. Its
  idempotency key maps onto the vendor's idempotency header.
- Concurrent calls with the same key may both reach the connector. The connector's unique
  constraint keeps the effect single. This is tested, and it's documented as a requirement
  for write connectors.
- Role-aware authorization (who may *cause* a high-risk call) is Phase 7. Today every
  approval-requiring tool is blocked outright.
