# Grounding review rubric (human)

The eval runner checks citations mechanically: the cited ID must exist and must have been
retrieved in that run. That does not show that the cited text says what the answer says.
This rubric is for a person who reads the answer next to the cited passages.

Each run writes `review.csv` with these columns: `case_id`, `question`,
`answer`, `cited_passages`. Fill in the last three:

| Column | Values |
|---|---|
| `supported` | `yes`: every factual claim appears in a cited passage. `partial`: some claims are supported, at least one is not. `no`: the main claim is not in the cited text. `n/a`: there is no factual claim (abstention, ticket status) |
| `reviewer` | Your initials |
| `notes` | Which claim is unsupported, if any |

Guidelines:

- Judge only against the cited passages, not against your own knowledge or other
  documents.
- A number must match exactly ($89 is not "about $90").
- Paraphrase is fine if the meaning is the same.
- Advice the passages don't contain (for example "you may also want to…") makes the answer
  `partial`.

Report the share of `yes` among rows that are not `n/a`, with the reviewer count. Until a
completed file is committed next to a run, semantic grounding for that run is **NOT RUN**.
Do not have a model fill this in and report it as human review.
