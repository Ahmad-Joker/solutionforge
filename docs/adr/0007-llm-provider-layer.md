# ADR-0007: Own LLM service layer over vendor SDKs

- Status: Accepted
- Date: 2026-10-01

## Context
Workflows need model calls with deterministic budget enforcement, per-attempt cost
attribution to tenants and executions, fallback between models and providers, structured
outputs, and testability without API keys. Frameworks (LangChain and similar) give some of
this, but they couple the codebase to their abstractions and hide retries.

## Decision
- A thin `LLMProvider` protocol has one method, `complete(request) -> response`, making one
  attempt with no retries. Adapters translate vendor errors into our taxonomy
  (`RateLimited`, `ProviderUnavailable`, `InvalidRequest`, `ProviderAuthError`, …).
- `LLMService` owns policy: retries with jittered exponential backoff (honouring
  `retry-after`), model fallback chains, a per-model circuit breaker, JSON-Schema
  validation with repair turns, budget pre-checks, and metering.
- The Anthropic adapter uses the **official SDK** with `max_retries=0`. If the SDK retried
  internally, one logical attempt could turn into several billed requests the ledger never sees.
- Money is **integer micro-USD** with ceiling rounding; there are no floats anywhere in cost math.
- A deterministic **mock provider** is a first-class provider, not a test double. It backs
  local development, CI, demos and the evaluation harness.

## Consequences
- The whole call path, failure cases included, is unit-testable. Adapter behaviour is tested
  through the real SDK with an HTTP mock transport.
- Adding a provider (OpenAI-compatible, Bedrock, local) means writing one adapter and adding
  its price rows. No call sites change.
- Budget checks read committed spend, so concurrent calls can overshoot a limit by at most
  (concurrency × one call's worst case). Strict reservations are in the backlog.
- Breaker state is per process. A shared breaker is in the backlog.
