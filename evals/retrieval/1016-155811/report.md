# Retrieval comparison

- **when**: 2026-10-16T15:58:11+00:00
- **questions**: 12
- **question_source**: evals/assistant/dev.jsonl (answerable, labelled sources)
- **k**: 3
- **strategies**: hybrid,dense (hybrid = dense + keyword, reciprocal rank fusion)
- **database**: PostgreSQL + pgvector
- **repeats**: 3
- **setup_seconds_including_ingestion**: 24.8

| Config (embedder:size:overlap:strategy) | Chunks | Hits | Hit rate @3 | Median latency (ms) | p95 latency (ms) |
|---|---|---|---|---|---|
| hashing-v1:800:120:hybrid | 93 | 11/12 | 0.92 | 53.62 | 70.9 |
| hashing-v1:800:120:dense | 93 | 11/12 | 0.92 | 28.11 | 46.43 |
| hashing-v1:200:30:hybrid | 103 | 10/12 | 0.83 | 50.4 | 62.57 |
| hashing-v1:200:30:dense | 103 | 11/12 | 0.92 | 33.58 | 45.36 |
| bge-small-en-v1.5:800:120:hybrid | 93 | 11/12 | 0.92 | 33.0 | 47.57 |
| bge-small-en-v1.5:800:120:dense | 93 | 12/12 | 1.00 | 27.0 | 41.62 |
| bge-small-en-v1.5:200:30:hybrid | 103 | 11/12 | 0.92 | 32.99 | 39.85 |
| bge-small-en-v1.5:200:30:dense | 103 | 12/12 | 1.00 | 32.3 | 57.21 |

n = 12 questions: a small sample, so one question is 8 points of hit rate.

## Misses

- `hashing-v1:800:120:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-FAQ-03#4', 'HP-SVC-01#3']
- `hashing-v1:800:120:dense` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-01#1', 'HP-SOP-02#1', 'HP-SVC-01#1']
- `hashing-v1:200:30:hybrid` dev-ans-03: wanted ['HP-SVC-05'], got ['HP-POL-01#2', 'HP-SVC-01#5', 'HP-POL-01#1']
- `hashing-v1:200:30:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-FAQ-03#4', 'HP-SVC-01#3']
- `hashing-v1:200:30:dense` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-01#1', 'HP-SOP-02#1', 'HP-SVC-01#2']
- `bge-small-en-v1.5:800:120:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-FAQ-04#3', 'HP-SOP-03#1']
- `bge-small-en-v1.5:200:30:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-FAQ-04#3', 'HP-SOP-03#1']
