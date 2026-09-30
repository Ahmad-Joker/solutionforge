# ADR-0001: Modular monolith in a monorepo

- Status: Accepted
- Date: 2026-10-01

## Context
SolutionForge has many concerns (auth, tenancy, workflows, RAG, tools, evaluation,
observability), and one developer builds it. Microservices would add network failure modes,
distributed transactions and deployment overhead long before there's any load to justify them.

## Decision
One Python package (`solutionforge`) deployed as two process types: the **API** and the
**worker**, both built from the same image. Internal boundaries are strict layers
(`api → services → domain/db/security`). Services never import FastAPI. The Next.js dashboard
is a separate app in the same repo.

## Consequences
- Business operations like "create execution and audit it" can use single-transaction consistency.
- One build and one migration history.
- Boundaries are only enforced by review for now. An import-linter contract is in the backlog.
- A module can be extracted later (e.g. ingestion workers) because services already avoid HTTP concerns.
