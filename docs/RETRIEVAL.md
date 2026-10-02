# Retrieval: design and measured results

## Pipeline

```
upload (POST …/documents, 202) ──► documents.status = pending
worker: claim (SKIP LOCKED + lease) ─► chunk (structure-aware, exact offsets)
       ─► embed (batched) ─► one transaction: replace chunks + status = ready
failure ─► retry with backoff ─► after 3 attempts: status = failed (dead letter) ─► POST …/retry
```

- **Chunking** (`retrieval/chunking.py`):
  - splits on paragraphs, then sentences, with a hard wrap as a last resort;
  - packs pieces up to `chunk_size` characters with `chunk_overlap` characters of carried
    context;
  - **never crosses a Markdown heading** and records the governing heading as `section`;
  - keeps exact `[char_start, char_end)` offsets.

  Tests check full coverage (no character is lost), exact offsets, and the size limit.
- **Embeddings.** `HashingEmbedder` is lexical feature hashing: 1024-dim, unigrams plus
  half-weighted bigrams, L2-normalised. It is deterministic and works offline. **It is not a
  semantic model.** A real embedding model plugs in behind the `Embedder` protocol.
- **Storage.** `chunks.embedding` is `vector(1024)` on PostgreSQL, with an HNSW index on
  cosine distance (`m=16, ef_construction=64`) and a GIN full-text expression index.
- **Strategies** (`retrieval/search.py`):
  - **dense:** pgvector `<=>`, with a relevance floor;
  - **keyword:** on PostgreSQL, `websearch_to_tsquery` parsing with its terms **OR-ed**,
    ranked by `ts_rank_cd`; Okapi BM25 elsewhere;
  - **hybrid:** Reciprocal Rank Fusion (k=60) over the top `max(4·k, 20)` of each list.

  Metadata filters are applied in SQL.

## Citations

The `grounded_answer` step:

1. labels sources `S1..Sn`;
2. uses an output schema that only admits those labels;
3. **re-verifies in code** that every cited label and every inline `[S#]` marker is a
   provided source, and that a substantive answer cites at least one;
4. gives one feedback turn, then fails the step;
5. maps labels back to `chunk_id`, `document_id` and character offsets.

The model therefore can't produce a citation that doesn't point at a retrieved chunk. If
retrieval returns nothing, no model is called: the answer is `insufficient_context`.

## Measured results

**Setup.**

- **Corpus:** 12 synthetic support articles and 24 labelled queries (`tests/fixtures/support_kb.json`).
- **Chunking:** `chunk_size=400`, `chunk_overlap=60`.
- **Embedder:** `HashingEmbedder`.
- **Relevance:** document-level; chunk hits are collapsed to documents.
- **Backend:** the portable path (exact cosine scan + Python BM25) on SQLite. It's produced
  by `tests/integration/test_knowledge.py::test_retrieval_benchmark` (run with `-s` to print).

| strategy | queries | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|---|
| dense | 24 | 0.896 | 1.000 | 1.000 | 0.958 |
| keyword | 24 | 0.979 | 1.000 | 1.000 | 1.000 |
| hybrid | 24 | 0.979 | 1.000 | 1.000 | 1.000 |

**PostgreSQL 16 + pgvector (production path).** Same corpus and queries, measured
2026-10-02 by the same test against `pgvector/pgvector:pg16`. Dense uses the HNSW index;
keyword uses PostgreSQL full-text search.

| strategy | queries | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|---|
| dense | 24 | 0.896 | 1.000 | 1.000 | 0.958 |
| keyword | 24 | 0.896 | 1.000 | 1.000 | 0.951 |
| hybrid | 24 | 0.938 | 1.000 | 1.000 | 0.972 |

> **Bug found by the first PostgreSQL run.** The keyword path originally used
> `websearch_to_tsquery` as-is. That ANDs every term, so a natural-language question
> matched only chunks containing *all* its words: keyword recall@5 was **0.583** (MRR
> 0.583), against 1.000 on the SQLite/BM25 path. The SQLite numbers had been hiding a
> production-only defect. Terms are now OR-ed, keeping websearch parsing (stemming, stop
> words, quoted phrases, negation). The PostgreSQL table above is after the fix. Keyword
> ranking still differs from BM25 (`ts_rank_cd` is cover density, not BM25), which is why
> the two backends don't produce identical numbers.
>
> A second backend difference: PostgreSQL's `english` text-search parser treats HTML/XML
> tags as markup, so words *inside* tags (`<img src=… onerror=…>`) aren't keyword-searchable
> there, while the BM25 path indexes them. Text between tags is indexed on both, and dense
> search sees everything.

### Dense relevance floor (calibration)

Nearest-neighbour search always returns *something*, so an off-topic question would still
send unrelated passages to the answer step. Offline measurement on the same corpus, plus 6
off-topic probe queries:

| floor | dense recall@5 | dense MRR | off-topic probes returning hits |
|---|---|---|---|
| 0.00 | 1.000 | 0.958 | 6/6 |
| 0.05 | 1.000 | 0.958 | 3/6 |
| **0.08 (default)** | **1.000** | **0.958** | **0/6** |
| 0.10 | 0.917 | 0.896 | 0/6 |
| 0.15 | 0.792 | 0.792 | 0/6 |

The highest off-topic score was 0.078, so the 0.08 margin is thin. The floor is specific to
this embedder and must be re-measured when the embedder changes. It's configurable per
search (`min_dense_score`).

## How to read these numbers

- The corpus is **small and synthetic**, and most queries share vocabulary with their target
  article. That favours keyword search and a lexical embedder. These are regression baselines
  for this codebase, not claims about real-world retrieval quality.
- Dense trails keyword at rank 1 because the embedder is lexical. A semantic embedder is
  expected to help most on paraphrases ("send something back" → returns). That will be
  measured, not assumed, once one is configured.
- **A counter-example from a live run.** For the paraphrase *"my parcel has not arrived and
  is late"*, hybrid search ranked *Delayed orders* only **3rd**, reached through keyword
  match. Dense ranked *Payment methods* first because both contain "not", and *How to return
  an item* second because both contain "parcel". That's typical of a lexical embedder on
  paraphrases, and it's why the fixture numbers above flatter dense retrieval. A semantic
  embedder is the highest-value next step for retrieval quality. Tuning stopwords to fix this
  one query would be overfitting.
- PostgreSQL uses `ts_rank_cd` (not BM25) and an *approximate* HNSW index, so its rankings
  can differ slightly. CI runs the same tests against PostgreSQL. Re-measure there before
  quoting production numbers.
