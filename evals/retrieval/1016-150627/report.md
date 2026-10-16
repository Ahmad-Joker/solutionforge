# Retrieval comparison

- **when**: 2026-10-16T15:06:27+00:00
- **questions**: 12
- **question_source**: evals/assistant/dev.jsonl (answerable, labelled sources)
- **k**: 3
- **strategies**: hybrid,dense (hybrid = dense + keyword, reciprocal rank fusion)
- **database**: SQLite (throwaway; dense search is brute-force in Python, not pgvector)
- **repeats**: 3
- **setup_seconds_including_ingestion**: 21.7

| Config (embedder:size:overlap:strategy) | Chunks | Hits | Hit rate @3 | Median latency (ms) | p95 latency (ms) |
|---|---|---|---|---|---|
| hashing-v1:800:120:hybrid | 93 | 11/12 | 0.92 | 74.12 | 88.87 |
| hashing-v1:800:120:dense | 93 | 11/12 | 0.92 | 75.56 | 82.18 |
| hashing-v1:200:30:hybrid | 103 | 11/12 | 0.92 | 95.19 | 117.52 |
| hashing-v1:200:30:dense | 103 | 11/12 | 0.92 | 76.49 | 92.99 |
| bge-small-en-v1.5:800:120:hybrid | 93 | 11/12 | 0.92 | 154.58 | 171.42 |
| bge-small-en-v1.5:800:120:dense | 93 | 12/12 | 1.00 | 141.14 | 156.54 |
| bge-small-en-v1.5:200:30:hybrid | 103 | 11/12 | 0.92 | 165.21 | 172.77 |
| bge-small-en-v1.5:200:30:dense | 103 | 12/12 | 1.00 | 153.49 | 168.76 |

n = 12 questions: a small sample, so one question is 8 points of hit rate.

## Misses

- `hashing-v1:800:120:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#4', 'HP-FAQ-03#2', 'HP-SVC-01#1']
- `hashing-v1:800:120:dense` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-01#1', 'HP-SOP-02#1', 'HP-SVC-01#1']
- `hashing-v1:200:30:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#4', 'HP-FAQ-03#2', 'HP-FAQ-01#1']
- `hashing-v1:200:30:dense` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-01#1', 'HP-SOP-02#1', 'HP-SVC-01#2']
- `bge-small-en-v1.5:800:120:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-SVC-03#4', 'HP-SOP-02#2']
- `bge-small-en-v1.5:200:30:hybrid` dev-ans-10: wanted ['HP-SVC-05'], got ['HP-FAQ-03#2', 'HP-SVC-03#4', 'HP-SOP-02#2']
