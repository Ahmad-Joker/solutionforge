# ADR-0010: pgvector retrieval with hybrid ranking and code-verified citations

- Status: Accepted
- Date: 2026-10-01

## Context
Answers must be grounded in tenant documents, with citations that are guaranteed to be real.
Retrieval should work offline for development and CI, and scale on the existing PostgreSQL.

## Decision
- **PostgreSQL + pgvector** (`vector(1024)`, HNSW cosine) and **Postgres full-text search**
  (GIN expression index) instead of a separate vector database or search engine. Hybrid
  ranking uses **Reciprocal Rank Fusion**, which needs no score normalisation across
  incomparable scales.
- A **lexical hashing embedder** is the default. It's deterministic, needs no dependency or
  network, and is honestly labelled as non-semantic. A semantic embedder plugs in via the
  `Embedder` protocol. Changing dimensions needs a migration.
- A **dense relevance floor**, calibrated by measurement (docs/RETRIEVAL.md) and
  embedder-specific, so off-topic questions retrieve nothing instead of noise.
- **Citations are enforced by code.** A label-enum output schema plus a post-generation
  verifier (labels, inline markers, a substantive answer must cite). Labels map back to chunk
  IDs and offsets.
- **Ingestion is a durable job** with a dead letter, using the same pattern as the workflow engine.

## Consequences
- One datastore to operate and secure, with transactional consistency between documents
  and chunks.
- Two retrieval code paths (PostgreSQL SQL and the portable scan). Parity is checked by
  running the same tests on both backends, and rankings may differ slightly
  (`ts_rank_cd` ≠ BM25; HNSW is approximate).
- OpenSearch, rerankers and semantic embedders remain options, to adopt when measurements
  justify them.
