# ADR-0003: Custom durable workflow engine (not LangGraph as the core)

- Status: Accepted
- Date: 2026-10-01

## Context
Workflows must survive worker crashes and server restarts, pause for human approval for hours
or days, be versioned immutably, be replayable for debugging, and enforce step, time, token and
cost budgets. Security policy must sit outside any model-controlled loop.

## Options
1. **LangGraph** as the engine. It's mature for agent graphs and has checkpointers, but it
   couples state shape, persistence and the approval model to the framework. Tenancy, budgets
   and policy would still need wrapping.
2. **Temporal**. Excellent durability, but a heavy extra platform for a single-developer deployment.
3. **Custom state machine on PostgreSQL.** A small engine: typed steps, a checkpoint per step
   in the same transaction as its ExecutionStep record, and `SKIP LOCKED` leasing.

## Decision
Option 3. The engine is a thin, well-tested core. Agentic behavior lives *inside* individual
step types (e.g. a bounded `agent` step), and a LangGraph-backed step type can be added later
without changing the durability, policy or budget model.

## Consequences
- We own correctness of retries, leases and resumption, so the tests must cover crash/resume paths.
- No framework lock-in. Each step's inputs, outputs, cost and latency are first-class rows,
  which the evaluation and observability features build on.
- Redis is an optimization for wake-up latency, not a durability dependency.
