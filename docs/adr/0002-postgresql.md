# ADR-0002: PostgreSQL (with pgvector) as the system of record

- Status: Accepted
- Date: 2026-10-01

## Context
The platform needs relational integrity (orgs, memberships, versions), durable workflow state,
JSON documents (execution state, tool arguments), full-text search, and vector search.

## Decision
PostgreSQL 16 with the `pgvector` extension, accessed through SQLAlchemy 2 (async, asyncpg) and
migrated with Alembic. JSONB stores flexible payloads, `tsvector` handles keyword search, and
`vector` + HNSW handles dense retrieval. OpenSearch is deferred until measurements show pgvector
is the bottleneck.

## Consequences
- One datastore to operate, back up and secure. Transactions cover state and audit together.
- Row-Level Security is available as a second tenant-isolation layer (ADR-0004).
- `SELECT … FOR UPDATE SKIP LOCKED` gives a durable job queue without a separate broker (ADR-0003).
- Tests run on SQLite locally for speed. CI runs the same suite on PostgreSQL, and
  Postgres-specific behavior (the audit trigger) is only verified there, so CI is authoritative.
