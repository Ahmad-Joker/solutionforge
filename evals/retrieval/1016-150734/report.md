# Retrieval comparison

- **when**: 2026-10-16T15:07:34+00:00
- **questions**: 12
- **question_source**: evals/assistant/dev.jsonl (answerable, labelled sources)
- **k**: 3
- **strategies**: hybrid (hybrid = dense + keyword, reciprocal rank fusion)
- **database**: SQLite (throwaway; dense search is brute-force in Python, not pgvector)
- **repeats**: 3
- **setup_seconds_including_ingestion**: 22.1

| Config (embedder:size:overlap:strategy) | Chunks | Hits | Hit rate @3 | Median latency (ms) | p95 latency (ms) |
|---|---|---|---|---|---|
| hashing-v1:800:120:hybrid | 93 | 11/12 | 0.92 | 75.67 | 101.97 |
| hashing-v1:400:60:hybrid | 94 | 11/12 | 0.92 | 77.64 | 101.96 |
| bge-small-en-v1.5:800:120:hybrid | 93 | 11/12 | 0.92 | 156.95 | 171.81 |
| bge-small-en-v1.5:400:60:hybrid | 94 | 11/12 | 0.92 | 152.47 | 166.29 |

n = 12 questions: a small sample, so one question is 8 points of hit rate.

## Misses

- `hashing-v1:800:120:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#4', 'HP-FAQ-03#2', 'HP-SVC-01#1']
- `hashing-v1:400:60:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#4', 'HP-FAQ-03#2', 'HP-FAQ-01#1']
- `bge-small-en-v1.5:800:120:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-SVC-03#4', 'HP-SOP-02#2']
- `bge-small-en-v1.5:400:60:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-SVC-03#4', 'HP-SOP-02#2']
