# ADR-0009: Schema-constrained action protocol for agents

- Status: Accepted
- Date: 2026-10-01

## Context
Agents must choose among tools, but every choice has to be validated, policy-gated, budgeted,
traced and reproducible in tests without API keys. Vendor-native tool calling differs across
providers (block types, parallel calls, forced tool choice restrictions on newer models) and
pulls loop control toward provider-specific code.

## Decision
The agent loop uses **structured output** as its action channel. Each turn returns exactly
one JSON action that validates against a schema we generate from the step's allowlist
(`call_tool` with an enumerated `tool`, or `final`). The runner, not the model or provider:

- re-checks the allowlist (defense in depth beyond the schema);
- sends the call through `ToolExecutor`, which handles argument validation, the risk policy,
  idempotency and the audit trail;
- turns failures into delimited `<observation>` messages, marked as untrusted data;
- enforces turn, tool-call, repeat and budget caps, and validates the final answer.

## Consequences
- The agent is provider-neutral and fully testable with the mock: tests script the worst
  case, a model that *obeys* an injected instruction, and show the policy gate still blocks
  refunds and emails.
- One action per turn means no parallel tool calls. That's a deliberate simplicity trade-off;
  latency-sensitive agents can add a native tool-use adapter behind the same runner interface.
- Security doesn't depend on the model resisting prompt injection. Model robustness only
  affects task quality, which the evaluation framework measures (Phase 11).
